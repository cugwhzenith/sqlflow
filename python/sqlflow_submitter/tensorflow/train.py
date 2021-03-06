# Copyright 2019 The SQLFlow Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
# Disable Tensorflow INFO and WARNING logs
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import sys, json
import tensorflow as tf
import functools
import sys
import numpy as np
import copy
try:
    import sqlflow_models
except:
    pass
from sqlflow_submitter.db import connect_with_data_source, db_generator, parseMaxComputeDSN
from . import metrics
from .input_fn import input_fn, pai_maxcompute_input_fn

SHUFFLE_SIZE = 1000
# TODO(shendiaomo): Remove after we fully upgrade to TF2.0
TF_VERSION_2 = True
TF_VERSION_PARTS = tf.__version__.split(".")
if int(TF_VERSION_PARTS[0]) == 1:
    TF_VERSION_2 = False

# Disable Tensorflow INFO and WARNING logs
if TF_VERSION_2:
    import logging
    tf.get_logger().setLevel(logging.ERROR)
else:
    tf.logging.set_verbosity(tf.logging.ERROR)
    from .pai_distributed import define_tf_flags, make_distributed_info_without_evaluator, dump_into_tf_config

def keras_train_and_save(estimator, model_params, save,
                         feature_column_names, feature_metas, label_meta,
                         datasource, select, validate_select,
                         batch_size, epochs, verbose, metric_names):
    # remove optimizer param from model_params and use it when call "compile()"
    optimizer = None
    if "optimizer" in model_params:
        optimizer = model_params["optimizer"]
        del model_params["optimizer"]
    classifier = estimator(**model_params)
    classifier_pkg = sys.modules[estimator.__module__]
    model_metrics = []
    if hasattr(classifier_pkg, "eval_metrics_fn"):
        metrics_functions = classifier_pkg.eval_metrics_fn()
        for key, func in metrics_functions.items():
            func.__name__ = key
            model_metrics.append(func)
    # use WITH specified metrics if it's not default.
    if metric_names != ["Accuracy"]:
        keras_metrics = metrics.get_keras_metrics(metric_names)
    else:
        if len(model_metrics) > 0:
            keras_metrics = model_metrics
        else:
            # default
            keras_metrics = metrics.get_keras_metrics(["Accuracy"])

    conn = connect_with_data_source(datasource)
    # FIXME(typhoonzero): find a way to cache to local file and avoid cache lockfile already exists issue.
    train_dataset = input_fn(select, conn, feature_column_names, feature_metas, label_meta)
    train_dataset = train_dataset.shuffle(SHUFFLE_SIZE).batch(batch_size)
    validate_dataset = input_fn(validate_select, conn, feature_column_names, feature_metas, label_meta).batch(batch_size)

    if optimizer is None:
        # use keras model default optimizer if optimizer is not specified in WITH clause.
        optimizer = classifier_pkg.optimizer()
    classifier.compile(optimizer=optimizer,
        loss=classifier_pkg.loss,
        metrics=keras_metrics)
    if hasattr(classifier, 'sqlflow_train_loop'):
        def flatten(feature, label):  # TODO(shendiaomo): Modify the cluster model to adapt the new input structure
            for k in feature:
                feature[k] = feature[k][0]
            return feature, [label]
        classifier.sqlflow_train_loop(train_dataset.map(flatten))
    else:
        if label_meta["feature_name"] != "":
            history = classifier.fit(train_dataset,
                                     epochs=epochs if epochs else classifier.default_training_epochs(),
                                     validation_data=validate_dataset,
                                     verbose=verbose)
        else:
            history = classifier.fit(train_dataset,
                                     epochs=epochs if epochs else classifier.default_training_epochs(),
                                     verbose=verbose)
        train_keys = []
        val_keys = []
        for k in history.history.keys():
            if k.startswith("val_"):
                val_keys.append(k)
            else:
                train_keys.append(k)
        print("====== Result for training set: ======")
        for k in train_keys:
            print("%s: %s" % (k, history.history[k][-1]))
        print("====== Result for validation set: ======")
        for k in val_keys:
            print("%s: %s" % (k, history.history[k][-1]))
    classifier.save_weights(save, save_format="h5")

def estimator_train_and_save(estimator, model_params, save,
                             is_pai, FLAGS, pai_table, pai_val_table,
                             feature_column_names, feature_metas, label_meta,
                             datasource, select, validate_select,
                             batch_size, epochs, verbose,
                             log_every_n_iter, train_max_steps, eval_start_delay_secs, eval_throttle_secs,
                             metric_names):
    classifier = estimator(**model_params)

    def train_input_fn():
        # FIXME(typhoonzero): find a way to cache to local file and avoid cache lockfile already exists issue.
        if is_pai:
            train_dataset = pai_maxcompute_input_fn(pai_table, datasource,
                feature_column_names, feature_metas, label_meta,
                len(FLAGS.worker_hosts), FLAGS.task_index)
        else:
            conn = connect_with_data_source(datasource)
            train_dataset = input_fn(select, conn, feature_column_names, feature_metas, label_meta)
        train_dataset = train_dataset.shuffle(SHUFFLE_SIZE).batch(batch_size).cache().repeat(epochs if epochs else 1)
        return train_dataset

    # do not add default Accuracy metric when using estimator to train, it will fail
    # when the estimator is a regressor, and estimator seems automatically add some
    # metrics. Only add additional metrics when user specified with `WITH`.
    if TF_VERSION_2 and metric_names != ["Accuracy"]:
        classifier = tf.estimator.add_metrics(classifier, metrics.get_tf_metrics(metric_names))

    train_spec = tf.estimator.TrainSpec(input_fn=lambda:train_input_fn(), max_steps=train_max_steps)
    def validate_input_fn():
        if is_pai:
            validate_dataset = pai_maxcompute_input_fn(pai_val_table, datasource,
                feature_column_names, feature_metas, label_meta,
                len(FLAGS.worker_hosts), FLAGS.task_index)
        else:
            conn = connect_with_data_source(datasource)
            validate_dataset = input_fn(validate_select, conn, feature_column_names, feature_metas, label_meta)
        validate_dataset = validate_dataset.batch(batch_size)
        return validate_dataset
    eval_spec = tf.estimator.EvalSpec(input_fn=lambda:validate_input_fn(), start_delay_secs=eval_start_delay_secs, throttle_secs=eval_throttle_secs)
    result = tf.estimator.train_and_evaluate(classifier, train_spec, eval_spec)
    # FIXME(typhoonzero): find out why pai will have result == None
    if not is_pai:
        print(result[0])

def train(is_keras_model,
          datasource,
          estimator,
          select,
          validate_select,
          feature_columns,
          feature_column_names,
          feature_metas={},
          label_meta={},
          model_params={},
          metric_names=["Accuracy"],
          save="",
          batch_size=1,
          epochs=1,
          verbose=0,
          train_max_steps=None,
          eval_start_delay_secs=0,
          eval_throttle_secs=0,
          save_checkpoints_steps=100,
          log_every_n_iter=10,
          is_pai=False,
          pai_table="",
          pai_val_table=""):
    assert validate_select != ""
    assert verbose >=0 and verbose <= 3
    if is_keras_model:
        if verbose == 1:
            tf.get_logger().setLevel((4-verbose) * 10)  # logging.INFO levels range from 10~40
    else:
        if TF_VERSION_2:
                tf.get_logger().setLevel((4-verbose) * 10)
        else:
            if verbose >= 2:
                tf.logging.set_verbosity(tf.logging.INFO)
    model_params.update(feature_columns)

    if is_keras_model:
        if not issubclass(estimator, tf.keras.Model):
            # functional model need field_metas parameter
            model_params["field_metas"] = feature_metas
        keras_train_and_save(estimator, model_params, save,
                         feature_column_names, feature_metas, label_meta,
                         datasource, select, validate_select,
                         batch_size, epochs, verbose, metric_names)
    else:
        is_distributed = False
        FLAGS = None
        # only support distributed training on PAI (TF version 1.x)
        if not TF_VERSION_2:
            FLAGS = define_tf_flags()
            if len(FLAGS.worker_hosts.split(",")) > 1:
                is_distributed = True
        if is_distributed:
            cluster, task_type, task_index = make_distributed_info_without_evaluator(FLAGS)
            dump_into_tf_config(cluster, task_type, task_index)
            dist_strategy = tf.contrib.distribute.ParameterServerStrategy()
            model_params["config"] = tf.estimator.RunConfig(save_checkpoints_steps=save_checkpoints_steps,
                train_distribute=dist_strategy, session_config=tf.ConfigProto(log_device_placement=True))
        else:
            model_params["config"] = tf.estimator.RunConfig(save_checkpoints_steps=save_checkpoints_steps)
        if is_pai:
            model_params["model_dir"] = FLAGS.checkpointDir
        else:
            model_params["model_dir"] = save
        estimator_train_and_save(estimator, model_params, save,
                             is_pai, FLAGS, pai_table, pai_val_table,
                             feature_column_names, feature_metas, label_meta,
                             datasource, select, validate_select,
                             batch_size, epochs, verbose,
                             log_every_n_iter, train_max_steps, eval_start_delay_secs, eval_throttle_secs,
                             metric_names)

    print("Done training")

