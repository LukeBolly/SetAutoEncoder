import tensorflow as tf
from models.stochastic_set_prior import StochasticSetPrior
from models.size_predictor import SizePredictor
from models.transformer_layers import TransformerLayer, PoolingMultiheadAttention


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

        self.pointwise_processing = PointwiseProcessing(preprocesing_dim, trans_dim)

        self.num_layers = num_layers
        self.transformer = [TransformerLayer(trans_dim, num_heads) for _ in range(num_layers)]
        self.transformer_pooling = PoolingMultiheadAttention(trans_dim, 1, 1)

    def call(self, set, mask):
        x = self.pointwise_processing(set)

        for i in range(self.num_layers):
            x = self.transformer[i](x, x, mask)

        merged = self.transformer_pooling(x, mask)

        return merged  # (batch_size, input_seq_len, d_model)


class SetDecoder(tf.keras.layers.Layer):
    def __init__(self, num_layers, trans_dim, num_heads):
        super(SetDecoder, self).__init__()
        # process initial set to transformer dimension
        self.embedding = tf.keras.layers.Conv1D(trans_dim, 1, kernel_initializer='glorot_uniform', use_bias=True,
                                                bias_initializer=tf.constant_initializer(0.1))\

        self.num_layers = num_layers
        self.transformer = [TransformerLayer(trans_dim, num_heads) for _ in range(num_layers)]

    def call(self, initial_set, mask):
        x = self.embedding(initial_set)

        for i in range(self.num_layers):
            x = self.transformer[i](x, x, mask)

        return x


class SetAutoEncoder(tf.keras.Model):
    def __init__(self, encoder_latent, transformer_layers, transformer_dim,
                 transformer_num_heads, num_element_features, size_pred_width, pad_value, max_set_size):
        super(SetAutoEncoder, self).__init__()

        self.pad_value = pad_value
        self.max_set_size = max_set_size
        self.num_element_features = num_element_features

        self._prior = StochasticSetPrior(num_element_features)

        self._encoder = SetEncoder(encoder_latent, transformer_layers, transformer_dim, transformer_num_heads)
        self._decoder = SetDecoder(transformer_layers, transformer_dim, transformer_num_heads)

        # initialise the output to predict points at the center of our canvas
        self._set_prediction = tf.keras.layers.Conv1D(num_element_features, 1, kernel_initializer='zeros',
                                                     bias_initializer=tf.keras.initializers.constant(0.5),
                                                     use_bias=True)

        self._size_predictor = SizePredictor(size_pred_width, max_set_size)

    def call(self, initial_set, sampled_set, sizes):
        # get the transformer mask []
        masked_values = tf.reshape(tf.cast(tf.math.logical_not(tf.sequence_mask(sizes, self.max_set_size)), tf.float32), [-1, 1, 1, self.max_set_size])

        # encode the input set
        encoded = self._encoder(initial_set, masked_values)  # pooled: [batch_size, num_features]

        # concat the encoded set vector onto each initial set element
        encoded_shaped = tf.tile(encoded, [1, self.max_set_size, 1])
        sampled_elements_conditioned = tf.concat([sampled_set, encoded_shaped], 2)

        pred_set_latent = self._decoder(sampled_elements_conditioned, masked_values)

        pred_set = self._set_prediction(pred_set_latent)
        return pred_set

    def sample_prior(self, sizes):
        total_elements = tf.reduce_sum(sizes)
        sampled_elements = self._prior(total_elements)  # [batch_size, max_set_size, num_features]
        return sampled_elements

    def sample_prior_batch(self, sizes):
        sampled_elements = self.sample_prior(sizes)
        samples_ragged = tf.RaggedTensor.from_row_lengths(sampled_elements, sizes)
        padded_samples = samples_ragged.to_tensor(default_value=self.pad_value,
                                                  shape=[sizes.shape[0], self.max_set_size, self.num_element_features])
        return padded_samples

    def encode_set(self, initial_set, sizes):
        return self._encoder(initial_set, sizes)

    def predict_size(self, embedding):
        sizes = self._size_predictor(embedding)
        sizes = tf.keras.activations.softmax(sizes, -1)
        return sizes

    def get_autoencoder_weights(self):
        return self._encoder.trainable_weights + \
               self._decoder.trainable_weights + \
               self._set_prediction.trainable_weights

    def get_prior_weights(self):
        return self._prior.trainable_weights

    def get_size_predictor_weights(self):
        return self._size_predictor.trainable_weights
