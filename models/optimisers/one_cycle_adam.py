import tensorflow as tf
import tensorflow_addons as tfa

# https://github.com/LukeBolly/OneCycleAdamW
class OneCycleAdamW(tfa.optimizers.AdamW):
    def __init__(self, learning_rate, weight_decay, cycle_length):
        self.one_cycle_schedule = OneCycleSchedule(cycle_length)

        lr = lambda: learning_rate * self.one_cycle_schedule(self.iterations)
        wd = lambda: weight_decay * self.one_cycle_schedule(self.iterations)
        momentum = lambda: self.one_cycle_schedule.get_momentum(self.iterations)

        super(OneCycleAdamW, self).__init__(learning_rate=lr, weight_decay=wd, beta_1=momentum, beta_2=0.99)


class OneCycleSchedule(tf.optimizers.schedules.LearningRateSchedule):
    def __init__(self, cycle_length):
        self._warmup_end_step = int(cycle_length * 0.1)
        self._max_end_step = self._warmup_end_step + int(cycle_length * 0.3)
        self._decay_end_step = self._max_end_step + int(cycle_length * 0.6)

    def __call__(self, step):
        def warmup():
            # interpolate between initial and max lr
            lr_factor = tf.cast(0.1 + 0.9 * (step / self._warmup_end_step), tf.float32)
            return lr_factor

        def max_lr():
            # remain at max for a period
            return tf.constant(1.0, tf.float32)

        def initial_decay():
            # decay at half the speed we warmed up at
            decay_step = step - self._max_end_step
            lr_factor = tf.cast(1 - 0.9 * (decay_step / (self._decay_end_step - self._max_end_step)), tf.float32)
            return lr_factor

        def final_decay():
            # then exponential decay from there
            final_step = tf.cast(step - self._decay_end_step, tf.float32)
            lr_factor = tf.cast(0.1 * tf.math.pow(1 - 1 / self._decay_end_step, final_step), tf.float32)
            return lr_factor

        learning_rate = tf.case([(tf.less_equal(step, self._warmup_end_step), warmup),
                                 (tf.less_equal(step, self._max_end_step), max_lr),
                                 (tf.less_equal(step, self._decay_end_step), initial_decay)],
                                default=final_decay)

        return learning_rate

    def get_momentum(self, step):
        def warmup():
            # interpolate between initial and max momentum
            momentum = tf.cast(0.95 - 0.1 * (step / self._warmup_end_step), tf.float32)
            return momentum

        def max_lr():
            # remain at max for a period
            return tf.constant(0.85, tf.float32)

        def initial_decay():
            # decay at half the speed we warmed up at
            decay_step = step - self._max_end_step
            momentum = tf.cast(0.85 + 0.1 * (decay_step / (self._decay_end_step - self._max_end_step)), tf.float32)
            return momentum

        def final_decay():
            # remain at highest momentum
            return tf.constant(0.95, tf.float32)

        learning_rate = tf.case([(tf.less_equal(step, self._warmup_end_step), warmup),
                                 (tf.less_equal(step, self._max_end_step), max_lr),
                                 (tf.less_equal(step, self._decay_end_step), initial_decay)],
                                default=final_decay)

        return learning_rate