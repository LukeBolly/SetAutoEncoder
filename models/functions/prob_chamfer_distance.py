import tensorflow as tf
from tensorflow_probability import distributions as tfd

# this is modified from tensorflows chamfer distance func
# https://github.com/tensorflow/graphics/blob/master/tensorflow_graphics/nn/loss/chamfer_distance.py


# modification of chamfer distance to calculate smallest log_prob between a set distribution and another set
# log_prob instead of huber loss as a distance metric
def prob_chamfer_distance(set_dists, set, sizes, max_size):

    # compare each element with every other element
    probs = []

    # definitely not ideal to operate row by row, but it takes a boatload of memory to allocate the full [batch, set_size, set_size, features] array all at once
    set_row = tf.split(set, max_size, axis=-2)

    for i in range(max_size):
        set_prob = set_dists.log_prob(set_row[i])
        probs.append(set_prob)

    log_probs = tf.stack(probs, axis=-2)

    # flatten our batch dimensions so we just have [batch, elements, elements]
    log_probs = tf.reshape(log_probs, (-1, max_size, max_size))

    # remove the padded values before finding the min distance, otherwise the model can abuse the padding to
    # achieve lower chamfer loss and not actually learn anything
    # slice off the known extras from our tensor, otherwise raggedTensor throws an error if the final ragged
    # tensor can be squeezed smaller than the initial size (ie. at least one row / column needs to be current size)
    sizes_flat = tf.reshape(sizes, (-1,))
    largest_unpadded_dim = tf.reduce_max(sizes_flat)
    log_probs_trimmed = log_probs[:, :largest_unpadded_dim, :largest_unpadded_dim]

    row_sizes = tf.repeat(sizes_flat, sizes_flat)
    log_probs = tf.RaggedTensor.from_tensor(log_probs_trimmed, lengths=(sizes_flat, row_sizes))

    minimum_square_distance_a_to_b = tf.reduce_max(input_tensor=log_probs, axis=-1)
    minimum_square_distance_b_to_a = tf.reduce_max(input_tensor=log_probs, axis=-2)

    setwise_distance = (tf.reduce_mean(input_tensor=minimum_square_distance_a_to_b, axis=-1) +
                        tf.reduce_mean(input_tensor=minimum_square_distance_b_to_a, axis=-1))

    out_shape = tf.shape(set)[:-2]
    batch_shaped = tf.reshape(setwise_distance, shape=out_shape)

    return batch_shaped


if __name__ == '__main__':
    logvar = 0.005

    # simple set to ensure math is checking out
    mean = tf.constant([[[0.5, 0.75], [0.1, 0.25], [0.35, 0.9]], [[0.4, 0.45], [0.5, 0.7], [0.8, 0.25]]], tf.float32)

    dist = tfd.Independent(tfd.Normal(mean, logvar), 1)

    closest_prob = dist.log_prob(mean)
    expected = (tf.reduce_mean(input_tensor=closest_prob, axis=-1) + tf.reduce_mean(input_tensor=closest_prob, axis=-1))

    # same set but with elements swapped, to make sure the minimum permutation is being found
    inverted_mean = tf.reverse(mean, axis=[1])

    # add some error to the means, ensure loss is higher
    mean_e = mean + 0.1
    dist_e = tfd.Independent(tfd.Normal(mean_e, logvar), 1)

    best = prob_chamfer_distance(dist, mean, [3, 3], 3)
    actual = prob_chamfer_distance(dist, inverted_mean, [3, 3], 3)
    worst = prob_chamfer_distance(dist_e, mean, [3, 3], 3)

    eq = tf.assert_equal(actual, expected)

    # check a set that should have imbalanced distance between a=>b, b=>a
    imb_mean = tf.constant([[[0.5, 0.75], [0.1, 0.25]]], tf.float32)
    imb_dist = tfd.Independent(tfd.Normal(imb_mean, 1), 1)

    true = tf.constant([[[0.5, 0.75], [0.5, 0.75]]], tf.float32)
    closest_prob = imb_dist.log_prob(true)

    # this set has two matching points on one side, with one match and one outlier on the other
    expected = closest_prob[0][0] + tf.reduce_mean([closest_prob[0][0], closest_prob[0][1]])

    actual = prob_chamfer_distance(imb_dist, true, [2], 2)

    eq = tf.assert_equal(actual, expected)

    # test a big ol set like what gets used in the sc environment
    mean = tf.random.normal([10, 10, 200, 190])
    sizes = tf.random.uniform([10, 10], 50, 150, dtype=tf.int32)

    dist = tfd.Independent(tfd.Normal(mean, 1), 1)
    out = prob_chamfer_distance(dist, mean, sizes)
    pass
