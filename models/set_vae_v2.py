import tensorflow as tf

from models.linear_set_prior import DeterministicSetPrior
from models.stochastic_set_prior import StochasticSetPrior
from models.size_predictor import SizePredictor
from models.transformer_layers import TransformerLayer, PoolingMultiheadAttention, PICASO, GeneralisedPICASO
import tensorflow_probability as tfp

tfd = tfp.distributions
tfkl = tf.keras.layers
tfpl = tfp.layers


class PointwiseProcessing(tf.keras.layers.Layer):
    def __init__(self, preprocesing_dim, out_dim):
        super(PointwiseProcessing, self).__init__()
        self.conv1 = tf.keras.layers.Conv1D(preprocesing_dim, 1, kernel_initializer='glorot_uniform', use_bias=True)
        self.conv2 = tf.keras.layers.Conv1D(out_dim, 1, kernel_initializer='glorot_uniform', use_bias=True)

    def call(self, set):
        x = self.conv1(set)
        x = tf.nn.leaky_relu(x)
        x = self.conv2(x)

        return x


class SetEncoder(tf.keras.layers.Layer):
    def __init__(self, preprocesing_dim, num_layers, trans_dim, num_heads):
        super(SetEncoder, self).__init__()

        self.trans_dim = trans_dim
        self.pointwise_processing = PointwiseProcessing(preprocesing_dim, trans_dim)

        self.num_layers = num_layers
        self.transformer = [TransformerLayer(trans_dim, num_heads) for _ in range(num_layers)]
        self.transformer_pooling = PICASO(trans_dim, 1, 1)

        latent_dim = 64
        _latent_prior = tfd.Independent(tfd.Normal(loc=tf.zeros(latent_dim), scale=1), reinterpreted_batch_ndims=1)

        self.out_parameterization1 = tfkl.Dense(tfpl.IndependentNormal.params_size(latent_dim), activation='relu')
        self.out_parameterization2 = tfkl.Dense(tfpl.IndependentNormal.params_size(latent_dim), activation=None)
        self.out_dist = tfpl.IndependentNormal(latent_dim, activity_regularizer=tfpl.KLDivergenceRegularizer(_latent_prior, weight=1.0))

    def call(self, set, mask):
        x = self.pointwise_processing(set)

        for i in range(self.num_layers):
            x = self.transformer[i](x, x, mask)

        merged = self.transformer_pooling(x, mask)
        merged = tf.reshape(merged, (-1, self.trans_dim))

        dist_params = self.out_parameterization1(merged)
        dist_params = self.out_parameterization2(dist_params)
        dist = self.out_dist(dist_params)

        return dist  # (batch_size, input_seq_len, d_model)


class SetDecoder(tf.keras.layers.Layer):
    def __init__(self, num_layers, trans_dim, num_heads):
        super(SetDecoder, self).__init__()

        self.num_layers = num_layers
        self.initial_dense = tf.keras.layers.Conv1D(trans_dim, 1, kernel_initializer='glorot_uniform', use_bias=True)

        self.condition_dense = [tf.keras.layers.Conv1D(trans_dim, 1, kernel_initializer='glorot_uniform', use_bias=True) for _ in range(num_layers)]
        self.transformer = [TransformerLayer(trans_dim, num_heads) for _ in range(num_layers)]
        self.transformer_pooling = PICASO(trans_dim, 1, 1)

    def call(self, initial_set, mask, conditioning):
        x = self.initial_dense(initial_set)

        for i in range(self.num_layers):
            current_pooled = self.transformer_pooling(x, mask)
            current_pooled = tf.tile(current_pooled, [1, tf.shape(x)[1], 1])

            x = tf.concat([x, conditioning, current_pooled], 2)     # add conditioning vector to each point
            x = self.condition_dense[i](x)                   # process set to transformer dimension

            x = self.transformer[i](x, x, mask)

        return x


class SetVariationalAutoEncoderV2(tf.keras.Model):
    def __init__(self, encoder_latent, transformer_layers, transformer_dim,
                 transformer_num_heads, num_element_features, size_pred_width, pad_value, max_set_size):
        super(SetVariationalAutoEncoderV2, self).__init__()

        self.pad_value = pad_value
        self.max_set_size = max_set_size
        self.num_element_features = num_element_features

        self._prior = DeterministicSetPrior(num_element_features, self.max_set_size)

        self._encoder = SetEncoder(encoder_latent, transformer_layers, transformer_dim, transformer_num_heads)

        self._decoder = SetDecoder(transformer_layers, transformer_dim, transformer_num_heads)

        # initialise the output to predict points at the center of our canvas
        self._set_prediction_mean = tf.keras.layers.Conv1D(num_element_features, 1, use_bias=True,
                                                           kernel_initializer='zeros',
                                                           bias_initializer=tf.keras.initializers.constant(0.5))

        self._size_predictor = SizePredictor(size_pred_width, max_set_size)

    @tf.function
    def call(self, initial_set, sampled_set, sizes, eval_mode=False):
        # get the transformer mask []
        masked_values = tf.reshape(tf.cast(tf.math.logical_not(tf.sequence_mask(sizes, self.max_set_size)), tf.float32), [-1, 1, 1, self.max_set_size])

        # encode the input set
        if eval_mode:
            encoded = self._encoder(initial_set, masked_values).mode()
        else:
            encoded = self._encoder(initial_set, masked_values).sample()  # pooled: [batch_size, num_features]

        # concat the encoded set vector onto each initial set element
        encoded_shaped = tf.expand_dims(encoded, 1)
        conditioning = tf.tile(encoded_shaped, [1, self.max_set_size, 1])

        pred_set_latent = self._decoder(sampled_set, masked_values, conditioning)

        pred = self._set_prediction_mean(pred_set_latent)
        return pred

    def sample_prior_batch(self, sizes):
        return self._prior(sizes)

    def encode_set(self, initial_set, sizes):
        # get the transformer mask []
        masked_values = tf.reshape(tf.cast(tf.math.logical_not(tf.sequence_mask(sizes, self.max_set_size)), tf.float32), [-1, 1, 1, self.max_set_size])

        return self._encoder(initial_set, masked_values)

    def decode_set(self, set_latent, initial_set, sizes):
        masked_values = tf.reshape(tf.cast(tf.math.logical_not(tf.sequence_mask(sizes, self.max_set_size)), tf.float32), [-1, 1, 1, self.max_set_size])

        encoded_shaped = tf.expand_dims(set_latent, 1)
        encoded_shaped = tf.tile(encoded_shaped, [1, self.max_set_size, 1])
        sampled_elements_conditioned = tf.concat([initial_set, encoded_shaped], 2)

        pred_set_latent = self._decoder(sampled_elements_conditioned, masked_values)

        mean = self._set_prediction_mean(pred_set_latent)

        dist = tfd.Normal(mean, 0.005)
        return tfd.Independent(dist, 1)

    def predict_size(self, embedding):
        sizes = self._size_predictor(embedding)
        sizes = tf.keras.activations.softmax(sizes, -1)
        return sizes

    def get_autoencoder_weights(self):
        return self._encoder.trainable_weights + \
               self._decoder.trainable_weights + \
               self._set_prediction_mean.trainable_weights

    def get_prior_weights(self):
        return self._prior.trainable_weights

    def get_size_predictor_weights(self):
        return self._size_predictor.trainable_weights
