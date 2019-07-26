import numpy as np
import os
import scipy
import shutil
import test
import time
import unittest

from tensorflowonspark import TFCluster, dfutil
from tensorflowonspark.pipeline import HasBatchSize, HasSteps, Namespace, TFEstimator, TFParams
from tensorflow.keras import Sequential
from tensorflow.keras.layers import Dense


class PipelineTest(test.SparkTest):
  @classmethod
  def setUpClass(cls):
    super(PipelineTest, cls).setUpClass()

    # create an artificial training dataset of two features with labels computed from known weights
    np.random.seed(1234)
    cls.features = np.random.rand(1000, 2)
    cls.weights = np.array([3.14, 1.618])
    cls.labels = np.matmul(cls.features, cls.weights)
    # convert to Python types for use with Spark DataFrames
    cls.train_examples = [(cls.features[i].tolist(), [cls.labels[i].item()]) for i in range(1000)]
    # create a simple test dataset
    cls.test_examples = [([1.0, 1.0], [0.0])]

    # define model_dir and export_dir for tests
    cls.model_dir = os.getcwd() + os.sep + "test_model"
    cls.export_dir = os.getcwd() + os.sep + "test_export"
    cls.tfrecord_dir = os.getcwd() + os.sep + "test_tfr"

  @classmethod
  def tearDownClass(cls):
    super(PipelineTest, cls).tearDownClass()

  def setUp(self):
    super(PipelineTest, self).setUp()
    # remove any prior test artifacts
    shutil.rmtree(self.model_dir, ignore_errors=True)
    shutil.rmtree(self.export_dir, ignore_errors=True)
    shutil.rmtree(self.tfrecord_dir, ignore_errors=True)

  def tearDown(self):
    # Note: don't clean up artifacts after test (in case we need to view/debug)
    pass

  def test_namespace(self):
    """Namespace class initializers"""
    # from dictionary
    d = {'string': 'foo', 'integer': 1, 'float': 3.14, 'array': [1, 2, 3], 'map': {'a': 1, 'b': 2}}
    n1 = Namespace(d)
    self.assertEqual(n1.string, 'foo')
    self.assertEqual(n1.integer, 1)
    self.assertEqual(n1.float, 3.14)
    self.assertEqual(n1.array, [1, 2, 3])
    self.assertEqual(n1.map, {'a': 1, 'b': 2})
    self.assertTrue('string' in n1)
    self.assertFalse('extra' in n1)

    # from namespace
    n2 = Namespace(n1)
    self.assertEqual(n2.string, 'foo')
    self.assertEqual(n2.integer, 1)
    self.assertEqual(n2.float, 3.14)
    self.assertEqual(n2.array, [1, 2, 3])
    self.assertEqual(n2.map, {'a': 1, 'b': 2})
    self.assertTrue('string' in n2)
    self.assertFalse('extra' in n2)

    # from argv list
    argv = ["--foo", "1", "--bar", "test", "--baz", "3.14"]
    n3 = Namespace(argv)
    self.assertEqual(n3.argv, argv)

  def test_TFParams(self):
    """Merging namespace args w/ ML Params"""
    class Foo(TFParams, HasBatchSize, HasSteps):
      def __init__(self, args):
        super(Foo, self).__init__()
        self.args = args

    n = Namespace({'a': 1, 'b': 2})
    f = Foo(n).setBatchSize(10).setSteps(100)
    combined_args = f.merge_args_params()
    expected_args = Namespace({'a': 1, 'b': 2, 'batch_size': 10, 'steps': 100})
    self.assertEqual(combined_args, expected_args)

  def test_spark_saved_model(self):
    """InputMode.SPARK TFEstimator w/ explicit saved_model export for TFModel inferencing"""

    # create a Spark DataFrame of training examples (features, labels)
    trainDF = self.spark.createDataFrame(self.train_examples, ['col1', 'col2']).repartition(3)

    # train and export model
    args = {}
    estimator = TFEstimator(self.get_function('spark/train'), args) \
                  .setInputMapping({'col1': 'x', 'col2': 'y_'}) \
                  .setModelDir(self.model_dir) \
                  .setExportDir(self.export_dir) \
                  .setClusterSize(self.num_workers) \
                  .setNumPS(0) \
                  .setBatchSize(1) \
                  .setEpochs(1)
    model = estimator.fit(trainDF)
    self.assertTrue(os.path.isdir(self.export_dir))

    # create a Spark DataFrame of test examples (features, labels)
    testDF = self.spark.createDataFrame(self.test_examples, ['c1', 'c2'])

    # test saved_model using exported signature
    model.setTagSet('serve') \
          .setSignatureDefKey('serving_default') \
          .setInputMapping({'c1': 'dense_input'}) \
          .setOutputMapping({'dense': 'cout'})
    preds = model.transform(testDF).head()                  # take first/only result
    pred = preds.cout[0]                                    # unpack scalar from tensor
    expected = np.sum(self.weights)
    self.assertAlmostEqual(pred, expected, 3)

    # test saved_model using custom/direct mapping
    model.setTagSet('serve') \
          .setSignatureDefKey(None) \
          .setInputMapping({'c1': 'dense_input'}) \
          .setOutputMapping({'dense/BiasAdd': 'cout'})
    preds = model.transform(testDF).head()                  # take first/only result
    pred = preds.cout[0]                                    # unpack pred scalar from tensor
    self.assertAlmostEqual(pred, expected, 3)

#  def test_spark_sparse_tensor(self):
#    """InputMode.SPARK feeding sparse tensors"""
#    def sparse_train(args, ctx):
#        import tensorflow as tf
#
#        # reset graph in case we're re-using a Spark python worker (during tests)
#        tf.compat.v1.reset_default_graph()
#
#        cluster, server = ctx.start_cluster_server(ctx)
#        if ctx.job_name == "ps":
#          server.join()
#        elif ctx.job_name == "worker":
#          with tf.device(tf.compat.v1.train.replica_device_setter(
#            worker_device="/job:worker/task:%d" % ctx.task_index,
#            cluster=cluster)):
#            y_ = tf.compat.v1.placeholder(tf.float32, name='y_label')
#            label = tf.identity(y_, name='label')
#
#            row_indices = tf.compat.v1.placeholder(tf.int64, name='x_row_indices')
#            col_indices = tf.compat.v1.placeholder(tf.int64, name='x_col_indices')
#            values = tf.compat.v1.placeholder(tf.float32, name='x_values')
#            indices = tf.stack([row_indices[0], col_indices[0]], axis=1)
#            data = values[0]
#
#            x = tf.SparseTensor(indices=indices, values=data, dense_shape=[args.batch_size, 10])
#            w = tf.Variable(tf.random.truncated_normal([10, 1]), name='w')
#            y = tf.sparse.sparse_dense_matmul(x, w, name='y')
#
#            global_step = tf.compat.v1.train.get_or_create_global_step()
#            cost = tf.reduce_mean(input_tensor=tf.square(y_ - y), name='cost')
#            optimizer = tf.compat.v1.train.GradientDescentOptimizer(0.1).minimize(cost, global_step)
#
#          with tf.compat.v1.train.MonitoredTrainingSession(master=server.target,
#                                                           is_chief=(ctx.task_index == 0),
#                                                           checkpoint_dir=args.model_dir,
#                                                           save_checkpoint_steps=20) as sess:
#            tf_feed = ctx.get_data_feed(input_mapping=args.input_mapping)
#            while not sess.should_stop() and not tf_feed.should_stop():
#              batch = tf_feed.next_batch(args.batch_size)
#              if len(batch) > 0:
#                print("batch: {}".format(batch))
#                feed = {y_: batch['y_label'],
#                        row_indices: batch['x_row_indices'],
#                        col_indices: batch['x_col_indices'],
#                        values: batch['x_values']}
#                _, pred, trained_weights = sess.run([optimizer, y, w], feed_dict=feed)
#                print("trained_weights: {}".format(trained_weights))
#            sess.close()
#
#          # wait for MonitoredTrainingSession to save last checkpoint
#          time.sleep(10)
#
#    args = {}
#    estimator = TFEstimator(sparse_train, args) \
#              .setInputMapping({'labels': 'y_label', 'row_indices': 'x_row_indices', 'col_indices': 'x_col_indices', 'values': 'x_values'}) \
#              .setInputMode(TFCluster.InputMode.SPARK) \
#              .setModelDir(self.model_dir) \
#              .setClusterSize(self.num_workers) \
#              .setNumPS(1) \
#              .setBatchSize(1)
#
#    model_weights = np.array([[1.0, 1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0]]).T
#    examples = [scipy.sparse.random(1, 10, density=0.5,) for i in range(200)]
#    rdd = self.sc.parallelize(examples).map(lambda e: ((e * model_weights).tolist()[0][0], e.row.tolist(), e.col.tolist(), e.data.tolist()))
#    df = rdd.toDF(["labels", "row_indices", "col_indices", "values"])
#    df.show(5)
#    model = estimator.fit(df)
#
#    model.setOutputMapping({'label': 'label', 'y/SparseTensorDenseMatMul': 'predictions'})
#    test_examples = [scipy.sparse.random(1, 10, density=0.5,) for i in range(50)]
#    test_rdd = self.sc.parallelize(test_examples).map(lambda e: ((e * model_weights).tolist()[0][0], e.row.tolist(), e.col.tolist(), e.data.tolist()))
#    test_df = test_rdd.toDF(["labels", "row_indices", "col_indices", "values"])
#    test_df.show(5)
#    preds = model.transform(test_df)
#    preds.show(5)

  def test_tf_column_filter(self):
    """InputMode.TENSORFLOW TFEstimator saving temporary TFRecords, filtered by input_mapping columns"""

    # create a Spark DataFrame of training examples (features, labels)
    trainDF = self.spark.createDataFrame(self.train_examples, ['col1', 'col2'])

    # and add some extra columns
    df = trainDF.withColumn('extra1', trainDF.col1)
    df = df.withColumn('extra2', trainDF.col2)
    self.assertEqual(len(df.columns), 4)
    df.show()

    # train model on selected columns
    args = {}
    estimator = TFEstimator(self.get_function('tf/train'), args) \
                  .setInputMapping({'col1': 'x', 'col2': 'y_'}) \
                  .setInputMode(TFCluster.InputMode.TENSORFLOW) \
                  .setExportDir(self.export_dir) \
                  .setTFRecordDir(self.tfrecord_dir) \
                  .setClusterSize(self.num_workers) \
                  .setNumPS(1) \
                  .setBatchSize(10)
    estimator.fit(df)
    self.assertTrue(os.path.isdir(self.export_dir))
    self.assertTrue(os.path.isdir(self.tfrecord_dir))

    # verify that temporarily-saved TFRecords have the columns we requested
    df_tmp = dfutil.loadTFRecords(self.sc, self.tfrecord_dir)
    df_tmp.show()

    self.assertEqual(df_tmp.columns, ['col1', 'col2'])

  def test_tf_saved_model(self):
    """InputMode.TENSORFLOW TFEstimator w/ a separate saved_model export function to add placeholders for InputMode.SPARK TFModel inferencing"""

    # create a Spark DataFrame of training examples (features, labels)
    trainDF = self.spark.createDataFrame(self.train_examples, ['col1', 'col2'])

    # train model
    args = {}
    estimator = TFEstimator(self.get_function('tf/train'), args) \
                  .setInputMapping({'col1': 'x', 'col2': 'y_'}) \
                  .setInputMode(TFCluster.InputMode.TENSORFLOW) \
                  .setExportDir(self.export_dir) \
                  .setTFRecordDir(self.tfrecord_dir) \
                  .setClusterSize(self.num_workers) \
                  .setNumPS(1) \
                  .setBatchSize(10)
    model = estimator.fit(trainDF)
    self.assertTrue(os.path.isdir(self.export_dir))

    # create a Spark DataFrame of test examples (features, labels)
    testDF = self.spark.createDataFrame(self.test_examples, ['c1', 'c2'])

    # test from saved_model
    model.setTagSet('serve') \
        .setSignatureDefKey('serving_default') \
        .setInputMapping({'c1': 'dense_input'}) \
        .setOutputMapping({'dense': 'cout'})
    preds = model.transform(testDF).head()                # take first/only result, e.g. [ Row(cout=[4.758000373840332])]
    pred1 = preds.cout[0]
    self.assertAlmostEqual(pred1, np.sum(self.weights), 5)

  def get_function(self, name):
    """Returns a TF map_function for tests (required to avoid serializing the parent module/class)"""

    def _spark_train(args, ctx):
      """Basic linear regression in a distributed TF cluster using InputMode.SPARK"""
      import tensorflow as tf
      from tensorflowonspark import TFNode

      model = Sequential()
      model.add(Dense(1, activation='linear', input_shape=(2,)))
      model.compile(optimizer=tf.keras.optimizers.Adam(lr=0.2), loss='mse', metrics=['mse'])
      model.summary()

      tf_feed = TFNode.DataFeed(ctx.mgr, input_mapping=args.input_mapping)
      while not tf_feed.should_stop():
        batch = tf_feed.next_batch(args.batch_size)
        if args.input_mapping:
          if len(batch['x']) > 0:
            model.fit(np.array(batch['x']), np.array(batch['y_']))

      if ctx.job_name == 'chief':
        print("saving checkpoint to: {}".format(args.model_dir))
        tf.saved_model.save(model, args.model_dir)
        # model.save_weights(args.model_dir + "/model", overwrite=True, save_format='tf')

        if args.export_dir:
          print("exporting model to: {}".format(args.export_dir))
          tf.keras.experimental.export_saved_model(model, args.export_dir)

    def _tf_train(args, ctx):
      """Basic linear regression in a distributed TF cluster using InputMode.TENSORFLOW"""
      import tensorflow as tf

      def _get_examples(num_rows, batch_size):
        """Generate test data"""
        for i in range(num_rows):
          features = tf.random.uniform([batch_size, 2])     # (batch_size x 2)
          weights = tf.constant([[3.14], [1.618]])          # (2, 1)
          labels = tf.matmul(features, weights)
          yield features, labels

      model = Sequential()
      model.add(Dense(1, activation='linear', input_shape=(2,)))
      model.compile(optimizer=tf.keras.optimizers.Adam(lr=0.2), loss='mse', metrics=['mse'])
      model.summary()

      model.fit_generator(_get_examples(1000, 10), steps_per_epoch=100, epochs=5)

      # export saved_model
      if ctx.job_name == 'chief' and args.export_dir:
        print("model weights: {}".format(model.get_weights()))
        print("exporting model to: {}".format(args.export_dir))
        tf.keras.experimental.export_saved_model(model, args.export_dir)

    if name == 'spark/train':
      return _spark_train
    elif name == 'tf/train':
      return _tf_train
    else:
      raise Exception("Unknown function name: {}".format(name))


if __name__ == '__main__':
  unittest.main()
