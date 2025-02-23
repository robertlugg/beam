#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import absolute_import
from __future__ import print_function

import collections
import logging
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import traceback
import typing
import unittest
import uuid
from builtins import range

# patches unittest.TestCase to be python3 compatible
import future.tests.base  # pylint: disable=unused-import
import hamcrest  # pylint: disable=ungrouped-imports
from hamcrest.core.matcher import Matcher
from hamcrest.core.string_description import StringDescription
from tenacity import retry
from tenacity import stop_after_attempt

import apache_beam as beam
from apache_beam.io import restriction_trackers
from apache_beam.metrics import monitoring_infos
from apache_beam.metrics.execution import MetricKey
from apache_beam.metrics.execution import MetricsEnvironment
from apache_beam.metrics.metricbase import MetricName
from apache_beam.options.pipeline_options import DebugOptions
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.portability import python_urns
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.runners.portability import fn_api_runner
from apache_beam.runners.worker import data_plane
from apache_beam.runners.worker import sdk_worker
from apache_beam.runners.worker import statesampler
from apache_beam.testing.synthetic_pipeline import SyntheticSDFAsSource
from apache_beam.testing.test_stream import TestStream
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import equal_to
from apache_beam.tools import utils
from apache_beam.transforms import userstate
from apache_beam.transforms import window

if statesampler.FAST_SAMPLER:
  DEFAULT_SAMPLING_PERIOD_MS = statesampler.DEFAULT_SAMPLING_PERIOD_MS
else:
  DEFAULT_SAMPLING_PERIOD_MS = 0


def _matcher_or_equal_to(value_or_matcher):
  """Pass-thru for matchers, and wraps value inputs in an equal_to matcher."""
  if value_or_matcher is None:
    return None
  if isinstance(value_or_matcher, Matcher):
    return value_or_matcher
  return hamcrest.equal_to(value_or_matcher)


def has_urn_and_labels(mi, urn, labels):
  """Returns true if it the monitoring_info contains the labels and urn."""
  def contains_labels(mi, labels):
    # Check all the labels and their values exist in the monitoring_info
    return all(item in mi.labels.items() for item in labels.items())
  return contains_labels(mi, labels) and mi.urn == urn


class FnApiRunnerTest(unittest.TestCase):

  def create_pipeline(self):
    return beam.Pipeline(runner=fn_api_runner.FnApiRunner())

  def test_assert_that(self):
    # TODO: figure out a way for fn_api_runner to parse and raise the
    # underlying exception.
    with self.assertRaisesRegex(Exception, 'Failed assert'):
      with self.create_pipeline() as p:
        assert_that(p | beam.Create(['a', 'b']), equal_to(['a']))

  def test_create(self):
    with self.create_pipeline() as p:
      assert_that(p | beam.Create(['a', 'b']), equal_to(['a', 'b']))

  def test_pardo(self):
    with self.create_pipeline() as p:
      res = (p
             | beam.Create(['a', 'bc'])
             | beam.Map(lambda e: e * 2)
             | beam.Map(lambda e: e + 'x'))
      assert_that(res, equal_to(['aax', 'bcbcx']))

  def test_pardo_metrics(self):

    class MyDoFn(beam.DoFn):

      def start_bundle(self):
        self.count = beam.metrics.Metrics.counter('ns1', 'elements')

      def process(self, element):
        self.count.inc(element)
        return [element]

    class MyOtherDoFn(beam.DoFn):

      def start_bundle(self):
        self.count = beam.metrics.Metrics.counter('ns2', 'elementsplusone')

      def process(self, element):
        self.count.inc(element + 1)
        return [element]

    with self.create_pipeline() as p:
      res = (p | beam.Create([1, 2, 3])
             | 'mydofn' >> beam.ParDo(MyDoFn())
             | 'myotherdofn' >> beam.ParDo(MyOtherDoFn()))
      p.run()
      if not MetricsEnvironment.METRICS_SUPPORTED:
        self.skipTest('Metrics are not supported.')

      counter_updates = [{'key': key, 'value': val}
                         for container in p.runner.metrics_containers()
                         for key, val in
                         container.get_updates().counters.items()]
      counter_values = [update['value'] for update in counter_updates]
      counter_keys = [update['key'] for update in counter_updates]
      assert_that(res, equal_to([1, 2, 3]))
      self.assertEqual(counter_values, [6, 9])
      self.assertEqual(counter_keys, [
          MetricKey('mydofn',
                    MetricName('ns1', 'elements')),
          MetricKey('myotherdofn',
                    MetricName('ns2', 'elementsplusone'))])

  def test_pardo_side_outputs(self):
    def tee(elem, *tags):
      for tag in tags:
        if tag in elem:
          yield beam.pvalue.TaggedOutput(tag, elem)
    with self.create_pipeline() as p:
      xy = (p
            | 'Create' >> beam.Create(['x', 'y', 'xy'])
            | beam.FlatMap(tee, 'x', 'y').with_outputs())
      assert_that(xy.x, equal_to(['x', 'xy']), label='x')
      assert_that(xy.y, equal_to(['y', 'xy']), label='y')

  def test_pardo_side_and_main_outputs(self):
    def even_odd(elem):
      yield elem
      yield beam.pvalue.TaggedOutput('odd' if elem % 2 else 'even', elem)
    with self.create_pipeline() as p:
      ints = p | beam.Create([1, 2, 3])
      named = ints | 'named' >> beam.FlatMap(
          even_odd).with_outputs('even', 'odd', main='all')
      assert_that(named.all, equal_to([1, 2, 3]), label='named.all')
      assert_that(named.even, equal_to([2]), label='named.even')
      assert_that(named.odd, equal_to([1, 3]), label='named.odd')

      unnamed = ints | 'unnamed' >> beam.FlatMap(even_odd).with_outputs()
      unnamed[None] | beam.Map(id)  # pylint: disable=expression-not-assigned
      assert_that(unnamed[None], equal_to([1, 2, 3]), label='unnamed.all')
      assert_that(unnamed.even, equal_to([2]), label='unnamed.even')
      assert_that(unnamed.odd, equal_to([1, 3]), label='unnamed.odd')

  def test_pardo_side_inputs(self):
    def cross_product(elem, sides):
      for side in sides:
        yield elem, side
    with self.create_pipeline() as p:
      main = p | 'main' >> beam.Create(['a', 'b', 'c'])
      side = p | 'side' >> beam.Create(['x', 'y'])
      assert_that(main | beam.FlatMap(cross_product, beam.pvalue.AsList(side)),
                  equal_to([('a', 'x'), ('b', 'x'), ('c', 'x'),
                            ('a', 'y'), ('b', 'y'), ('c', 'y')]))

  def test_pardo_windowed_side_inputs(self):
    with self.create_pipeline() as p:
      # Now with some windowing.
      pcoll = p | beam.Create(list(range(10))) | beam.Map(
          lambda t: window.TimestampedValue(t, t))
      # Intentionally choosing non-aligned windows to highlight the transition.
      main = pcoll | 'WindowMain' >> beam.WindowInto(window.FixedWindows(5))
      side = pcoll | 'WindowSide' >> beam.WindowInto(window.FixedWindows(7))
      res = main | beam.Map(lambda x, s: (x, sorted(s)),
                            beam.pvalue.AsList(side))
      assert_that(
          res,
          equal_to([
              # The window [0, 5) maps to the window [0, 7).
              (0, list(range(7))),
              (1, list(range(7))),
              (2, list(range(7))),
              (3, list(range(7))),
              (4, list(range(7))),
              # The window [5, 10) maps to the window [7, 14).
              (5, list(range(7, 10))),
              (6, list(range(7, 10))),
              (7, list(range(7, 10))),
              (8, list(range(7, 10))),
              (9, list(range(7, 10)))]),
          label='windowed')

  def test_flattened_side_input(self, with_transcoding=True):
    with self.create_pipeline() as p:
      main = p | 'main' >> beam.Create([None])
      side1 = p | 'side1' >> beam.Create([('a', 1)])
      side2 = p | 'side2' >> beam.Create([('b', 2)])
      if with_transcoding:
        # Also test non-matching coder types (transcoding required)
        third_element = [('another_type')]
      else:
        third_element = [('b', 3)]
      side3 = p | 'side3' >> beam.Create(third_element)
      side = (side1, side2) | beam.Flatten()
      assert_that(
          main | beam.Map(lambda a, b: (a, b), beam.pvalue.AsDict(side)),
          equal_to([(None, {'a': 1, 'b': 2})]),
          label='CheckFlattenAsSideInput')
      assert_that(
          (side, side3) | 'FlattenAfter' >> beam.Flatten(),
          equal_to([('a', 1), ('b', 2)] + third_element),
          label='CheckFlattenOfSideInput')

  def test_gbk_side_input(self):
    with self.create_pipeline() as p:
      main = p | 'main' >> beam.Create([None])
      side = p | 'side' >> beam.Create([('a', 1)]) | beam.GroupByKey()
      assert_that(
          main | beam.Map(lambda a, b: (a, b), beam.pvalue.AsDict(side)),
          equal_to([(None, {'a': [1]})]))

  def test_multimap_side_input(self):
    with self.create_pipeline() as p:
      main = p | 'main' >> beam.Create(['a', 'b'])
      side = (p | 'side' >> beam.Create([('a', 1), ('b', 2), ('a', 3)])
              # TODO(BEAM-4782): Obviate the need for this map.
              | beam.Map(lambda kv: (kv[0], kv[1])))
      assert_that(
          main | beam.Map(lambda k, d: (k, sorted(d[k])),
                          beam.pvalue.AsMultiMap(side)),
          equal_to([('a', [1, 3]), ('b', [2])]))

  def test_multimap_side_input_type_coercion(self):
    with self.create_pipeline() as p:
      main = p | 'main' >> beam.Create(['a', 'b'])
      # The type of this side-input is forced to Any (overriding type
      # inference). Without type coercion to Tuple[Any, Any], the usage of this
      # side-input in AsMultiMap() below should fail.
      side = (p | 'side' >> beam.Create([('a', 1), ('b', 2), ('a', 3)])
              .with_output_types(typing.Any))
      assert_that(
          main | beam.Map(lambda k, d: (k, sorted(d[k])),
                          beam.pvalue.AsMultiMap(side)),
          equal_to([('a', [1, 3]), ('b', [2])]))

  def test_pardo_unfusable_side_inputs(self):
    def cross_product(elem, sides):
      for side in sides:
        yield elem, side
    with self.create_pipeline() as p:
      pcoll = p | beam.Create(['a', 'b'])
      assert_that(
          pcoll | beam.FlatMap(cross_product, beam.pvalue.AsList(pcoll)),
          equal_to([('a', 'a'), ('a', 'b'), ('b', 'a'), ('b', 'b')]))

    with self.create_pipeline() as p:
      pcoll = p | beam.Create(['a', 'b'])
      derived = ((pcoll,) | beam.Flatten()
                 | beam.Map(lambda x: (x, x))
                 | beam.GroupByKey()
                 | 'Unkey' >> beam.Map(lambda kv: kv[0]))
      assert_that(
          pcoll | beam.FlatMap(cross_product, beam.pvalue.AsList(derived)),
          equal_to([('a', 'a'), ('a', 'b'), ('b', 'a'), ('b', 'b')]))

  def test_pardo_state_only(self):
    index_state_spec = userstate.CombiningValueStateSpec('index', sum)

    # TODO(ccy): State isn't detected with Map/FlatMap.
    class AddIndex(beam.DoFn):
      def process(self, kv, index=beam.DoFn.StateParam(index_state_spec)):
        k, v = kv
        index.add(1)
        yield k, v, index.read()

    inputs = [('A', 'a')] * 2 + [('B', 'b')] * 3
    expected = [('A', 'a', 1),
                ('A', 'a', 2),
                ('B', 'b', 1),
                ('B', 'b', 2),
                ('B', 'b', 3)]

    with self.create_pipeline() as p:
      assert_that(p | beam.Create(inputs) | beam.ParDo(AddIndex()),
                  equal_to(expected))

  @unittest.skip('TestStream not yet supported')
  def test_teststream_pardo_timers(self):
    timer_spec = userstate.TimerSpec('timer', userstate.TimeDomain.WATERMARK)

    class TimerDoFn(beam.DoFn):
      def process(self, element, timer=beam.DoFn.TimerParam(timer_spec)):
        unused_key, ts = element
        timer.set(ts)
        timer.set(2 * ts)

      @userstate.on_timer(timer_spec)
      def process_timer(self):
        yield 'fired'

    ts = (TestStream()
          .add_elements([('k1', 10)])  # Set timer for 20
          .advance_watermark_to(100)
          .add_elements([('k2', 100)])  # Set timer for 200
          .advance_watermark_to(1000))

    with self.create_pipeline() as p:
      _ = (
          p
          | ts
          | beam.ParDo(TimerDoFn())
          | beam.Map(lambda x, ts=beam.DoFn.TimestampParam: (x, ts)))

      #expected = [('fired', ts) for ts in (20, 200)]
      #assert_that(actual, equal_to(expected))

  def test_pardo_timers(self):
    timer_spec = userstate.TimerSpec('timer', userstate.TimeDomain.WATERMARK)

    class TimerDoFn(beam.DoFn):
      def process(self, element, timer=beam.DoFn.TimerParam(timer_spec)):
        unused_key, ts = element
        timer.set(ts)
        timer.set(2 * ts)

      @userstate.on_timer(timer_spec)
      def process_timer(self):
        yield 'fired'

    with self.create_pipeline() as p:
      actual = (
          p
          | beam.Create([('k1', 10), ('k2', 100)])
          | beam.ParDo(TimerDoFn())
          | beam.Map(lambda x, ts=beam.DoFn.TimestampParam: (x, ts)))

      expected = [('fired', ts) for ts in (20, 200)]
      assert_that(actual, equal_to(expected))

  def test_pardo_timers_clear(self):
    if type(self).__name__ != 'FlinkRunnerTest':
      # FnApiRunner fails to wire multiple timer collections
      # this method can replace test_pardo_timers when the issue is fixed
      self.skipTest('BEAM-7074: Multiple timer definitions not supported.')

    timer_spec = userstate.TimerSpec('timer', userstate.TimeDomain.WATERMARK)
    clear_timer_spec = userstate.TimerSpec('clear_timer',
                                           userstate.TimeDomain.WATERMARK)

    class TimerDoFn(beam.DoFn):
      def process(self, element, timer=beam.DoFn.TimerParam(timer_spec),
                  clear_timer=beam.DoFn.TimerParam(clear_timer_spec)):
        unused_key, ts = element
        timer.set(ts)
        timer.set(2 * ts)
        clear_timer.set(ts)
        clear_timer.clear()

      @userstate.on_timer(timer_spec)
      def process_timer(self):
        yield 'fired'

      @userstate.on_timer(clear_timer_spec)
      def process_clear_timer(self):
        yield 'should not fire'

    with self.create_pipeline() as p:
      actual = (
          p
          | beam.Create([('k1', 10), ('k2', 100)])
          | beam.ParDo(TimerDoFn())
          | beam.Map(lambda x, ts=beam.DoFn.TimestampParam: (x, ts)))

      expected = [('fired', ts) for ts in (20, 200)]
      assert_that(actual, equal_to(expected))

  def test_pardo_state_timers(self):
    self._run_pardo_state_timers(False)

  def test_windowed_pardo_state_timers(self):
    self._run_pardo_state_timers(True)

  def _run_pardo_state_timers(self, windowed):
    state_spec = userstate.BagStateSpec('state', beam.coders.StrUtf8Coder())
    timer_spec = userstate.TimerSpec('timer', userstate.TimeDomain.WATERMARK)
    elements = list('abcdefgh')
    buffer_size = 3

    class BufferDoFn(beam.DoFn):
      def process(self,
                  kv,
                  ts=beam.DoFn.TimestampParam,
                  timer=beam.DoFn.TimerParam(timer_spec),
                  state=beam.DoFn.StateParam(state_spec)):
        _, element = kv
        state.add(element)
        buffer = state.read()
        # For real use, we'd keep track of this size separately.
        if len(list(buffer)) >= 3:
          state.clear()
          yield buffer
        else:
          timer.set(ts + 1)

      @userstate.on_timer(timer_spec)
      def process_timer(self, state=beam.DoFn.StateParam(state_spec)):
        buffer = state.read()
        state.clear()
        yield buffer

    def is_buffered_correctly(actual):
      # Pickling self in the closure for asserts gives errors (only on jenkins).
      self = FnApiRunnerTest('__init__')
      # Acutal should be a grouping of the inputs into batches of size
      # at most buffer_size, but the actual batching is nondeterministic
      # based on ordering and trigger firing timing.
      self.assertEqual(sorted(sum((list(b) for b in actual), [])), elements)
      self.assertEqual(max(len(list(buffer)) for buffer in actual), buffer_size)
      if windowed:
        # Elements were assigned to windows based on their parity.
        # Assert that each grouping consists of elements belonging to the
        # same window to ensure states and timers were properly partitioned.
        for b in actual:
          parity = set(ord(e) % 2 for e in b)
          self.assertEqual(1, len(parity), b)

    with self.create_pipeline() as p:
      actual = (
          p
          | beam.Create(elements)
          # Send even and odd elements to different windows.
          | beam.Map(lambda e: window.TimestampedValue(e, ord(e) % 2))
          | beam.WindowInto(window.FixedWindows(1) if windowed
                            else window.GlobalWindows())
          | beam.Map(lambda x: ('key', x))
          | beam.ParDo(BufferDoFn()))

      assert_that(actual, is_buffered_correctly)

  def test_sdf(self):

    class ExpandingStringsDoFn(beam.DoFn):
      def process(
          self,
          element,
          restriction_tracker=beam.DoFn.RestrictionParam(
              ExpandStringsProvider())):
        assert isinstance(
            restriction_tracker,
            restriction_trackers.OffsetRestrictionTracker), restriction_tracker
        cur = restriction_tracker.start_position()
        while restriction_tracker.try_claim(cur):
          yield element[cur]
          cur += 1

    with self.create_pipeline() as p:
      data = ['abc', 'defghijklmno', 'pqrstuv', 'wxyz']
      actual = (
          p
          | beam.Create(data)
          | beam.ParDo(ExpandingStringsDoFn()))
      assert_that(actual, equal_to(list(''.join(data))))

  def test_sdf_with_sdf_initiated_checkpointing(self):

    counter = beam.metrics.Metrics.counter('ns', 'my_counter')

    class ExpandStringsDoFn(beam.DoFn):
      def process(
          self,
          element,
          restriction_tracker=beam.DoFn.RestrictionParam(
              ExpandStringsProvider())):
        assert isinstance(
            restriction_tracker,
            restriction_trackers.OffsetRestrictionTracker), restriction_tracker
        cur = restriction_tracker.start_position()
        while restriction_tracker.try_claim(cur):
          counter.inc()
          yield element[cur]
          if cur % 2 == 1:
            restriction_tracker.defer_remainder()
            return
          cur += 1

    with self.create_pipeline() as p:
      data = ['abc', 'defghijklmno', 'pqrstuv', 'wxyz']
      actual = (
          p
          | beam.Create(data)
          | beam.ParDo(ExpandStringsDoFn()))

      assert_that(actual, equal_to(list(''.join(data))))

    if isinstance(p.runner, fn_api_runner.FnApiRunner):
      res = p.runner._latest_run_result
      counters = res.metrics().query(beam.metrics.MetricsFilter())['counters']
      self.assertEqual(1, len(counters))
      self.assertEqual(counters[0].committed, len(''.join(data)))

  def test_group_by_key(self):
    with self.create_pipeline() as p:
      res = (p
             | beam.Create([('a', 1), ('a', 2), ('b', 3)])
             | beam.GroupByKey()
             | beam.Map(lambda k_vs: (k_vs[0], sorted(k_vs[1]))))
      assert_that(res, equal_to([('a', [1, 2]), ('b', [3])]))

  # Runners may special case the Reshuffle transform urn.
  def test_reshuffle(self):
    with self.create_pipeline() as p:
      assert_that(p | beam.Create([1, 2, 3]) | beam.Reshuffle(),
                  equal_to([1, 2, 3]))

  def test_flatten(self, with_transcoding=True):
    with self.create_pipeline() as p:
      if with_transcoding:
        # Additional element which does not match with the first type
        additional = [ord('d')]
      else:
        additional = ['d']
      res = (p | 'a' >> beam.Create(['a']),
             p | 'bc' >> beam.Create(['b', 'c']),
             p | 'd' >> beam.Create(additional)) | beam.Flatten()
      assert_that(res, equal_to(['a', 'b', 'c'] + additional))

  def test_combine_per_key(self):
    with self.create_pipeline() as p:
      res = (p
             | beam.Create([('a', 1), ('a', 2), ('b', 3)])
             | beam.CombinePerKey(beam.combiners.MeanCombineFn()))
      assert_that(res, equal_to([('a', 1.5), ('b', 3.0)]))

  def test_read(self):
    # Can't use NamedTemporaryFile as a context
    # due to https://bugs.python.org/issue14243
    temp_file = tempfile.NamedTemporaryFile(delete=False)
    try:
      temp_file.write(b'a\nb\nc')
      temp_file.close()
      with self.create_pipeline() as p:
        assert_that(p | beam.io.ReadFromText(temp_file.name),
                    equal_to(['a', 'b', 'c']))
    finally:
      os.unlink(temp_file.name)

  def test_windowing(self):
    with self.create_pipeline() as p:
      res = (p
             | beam.Create([1, 2, 100, 101, 102])
             | beam.Map(lambda t: window.TimestampedValue(('k', t), t))
             | beam.WindowInto(beam.transforms.window.Sessions(10))
             | beam.GroupByKey()
             | beam.Map(lambda k_vs1: (k_vs1[0], sorted(k_vs1[1]))))
      assert_that(res, equal_to([('k', [1, 2]), ('k', [100, 101, 102])]))

  def test_large_elements(self):
    with self.create_pipeline() as p:
      big = (p
             | beam.Create(['a', 'a', 'b'])
             | beam.Map(lambda x: (x, x * data_plane._DEFAULT_FLUSH_THRESHOLD)))

      side_input_res = (
          big
          | beam.Map(lambda x, side: (x[0], side.count(x[0])),
                     beam.pvalue.AsList(big | beam.Map(lambda x: x[0]))))
      assert_that(side_input_res,
                  equal_to([('a', 2), ('a', 2), ('b', 1)]), label='side')

      gbk_res = (
          big
          | beam.GroupByKey()
          | beam.Map(lambda x: x[0]))
      assert_that(gbk_res, equal_to(['a', 'b']), label='gbk')

  def test_error_message_includes_stage(self):
    with self.assertRaises(BaseException) as e_cm:
      with self.create_pipeline() as p:
        def raise_error(x):
          raise RuntimeError('x')
        # pylint: disable=expression-not-assigned
        (p
         | beam.Create(['a', 'b'])
         | 'StageA' >> beam.Map(lambda x: x)
         | 'StageB' >> beam.Map(lambda x: x)
         | 'StageC' >> beam.Map(raise_error)
         | 'StageD' >> beam.Map(lambda x: x))
    message = e_cm.exception.args[0]
    self.assertIn('StageC', message)
    self.assertNotIn('StageB', message)

  def test_error_traceback_includes_user_code(self):

    def first(x):
      return second(x)

    def second(x):
      return third(x)

    def third(x):
      raise ValueError('x')

    try:
      with self.create_pipeline() as p:
        p | beam.Create([0]) | beam.Map(first)  # pylint: disable=expression-not-assigned
    except Exception:  # pylint: disable=broad-except
      message = traceback.format_exc()
    else:
      raise AssertionError('expected exception not raised')

    self.assertIn('first', message)
    self.assertIn('second', message)
    self.assertIn('third', message)

  def test_no_subtransform_composite(self):

    class First(beam.PTransform):
      def expand(self, pcolls):
        return pcolls[0]

    with self.create_pipeline() as p:
      pcoll_a = p | 'a' >> beam.Create(['a'])
      pcoll_b = p | 'b' >> beam.Create(['b'])
      assert_that((pcoll_a, pcoll_b) | First(), equal_to(['a']))

  def test_metrics(self):
    p = self.create_pipeline()
    if not isinstance(p.runner, fn_api_runner.FnApiRunner):
      # This test is inherited by others that may not support the same
      # internal way of accessing progress metrics.
      self.skipTest('Metrics not supported.')

    counter = beam.metrics.Metrics.counter('ns', 'counter')
    distribution = beam.metrics.Metrics.distribution('ns', 'distribution')
    gauge = beam.metrics.Metrics.gauge('ns', 'gauge')

    pcoll = p | beam.Create(['a', 'zzz'])
    # pylint: disable=expression-not-assigned
    pcoll | 'count1' >> beam.FlatMap(lambda x: counter.inc())
    pcoll | 'count2' >> beam.FlatMap(lambda x: counter.inc(len(x)))
    pcoll | 'dist' >> beam.FlatMap(lambda x: distribution.update(len(x)))
    pcoll | 'gauge' >> beam.FlatMap(lambda x: gauge.set(3))

    res = p.run()
    res.wait_until_finish()
    c1, = res.metrics().query(beam.metrics.MetricsFilter().with_step('count1'))[
        'counters']
    self.assertEqual(c1.committed, 2)
    c2, = res.metrics().query(beam.metrics.MetricsFilter().with_step('count2'))[
        'counters']
    self.assertEqual(c2.committed, 4)
    dist, = res.metrics().query(beam.metrics.MetricsFilter().with_step('dist'))[
        'distributions']
    gaug, = res.metrics().query(
        beam.metrics.MetricsFilter().with_step('gauge'))['gauges']
    self.assertEqual(
        dist.committed.data, beam.metrics.cells.DistributionData(4, 2, 1, 3))
    self.assertEqual(dist.committed.mean, 2.0)
    self.assertEqual(gaug.committed.value, 3)

  def test_callbacks_with_exception(self):
    elements_list = ['1', '2']

    def raise_expetion():
      raise Exception('raise exception when calling callback')

    class FinalizebleDoFnWithException(beam.DoFn):

      def process(
          self,
          element,
          bundle_finalizer=beam.DoFn.BundleFinalizerParam):
        bundle_finalizer.register(raise_expetion)
        yield element

    with self.create_pipeline() as p:
      res = (p
             | beam.Create(elements_list)
             | beam.ParDo(FinalizebleDoFnWithException()))
      assert_that(res, equal_to(['1', '2']))

  def test_register_finalizations(self):
    event_recorder = EventRecorder(tempfile.gettempdir())
    elements_list = ['2', '1']

    class FinalizableDoFn(beam.DoFn):
      def process(
          self,
          element,
          bundle_finalizer=beam.DoFn.BundleFinalizerParam):
        bundle_finalizer.register(lambda: event_recorder.record(element))
        yield element

    with self.create_pipeline() as p:
      res = (p
             | beam.Create(elements_list)
             | beam.ParDo(FinalizableDoFn()))

      assert_that(res, equal_to(elements_list))

    results = event_recorder.events()
    event_recorder.cleanup()
    self.assertEqual(results, sorted(elements_list))

  def test_sdf_synthetic_source(self):
    common_attrs = {
        'key_size': 1,
        'value_size': 1,
        'initial_splitting_num_bundles': 2,
        'initial_splitting_desired_bundle_size': 2,
        'sleep_per_input_record_sec': 0,
        'initial_splitting': 'const'
    }
    num_source_description = 5
    min_num_record = 10
    max_num_record = 20

    # pylint: disable=unused-variable
    source_descriptions = ([dict(
        {'num_records': random.randint(min_num_record, max_num_record)},
        **common_attrs) for i in range(0, num_source_description)])
    total_num_records = 0
    for source in source_descriptions:
      total_num_records += source['num_records']

    with self.create_pipeline() as p:
      res = (p
             | beam.Create(source_descriptions)
             | beam.ParDo(SyntheticSDFAsSource())
             | beam.combiners.Count.Globally())
      assert_that(res, equal_to([total_num_records]))


# These tests are kept in a separate group so that they are
# not ran in the FnApiRunnerTestWithBundleRepeat which repeats
# bundle processing. This breaks the byte sampling metrics as
# it makes the probability of sampling far too small
# upon repeating bundle processing due to unncessarily incrementing
# the sampling counter.
class FnApiRunnerMetricsTest(unittest.TestCase):

  def assert_has_counter(
      self, monitoring_infos, urn, labels, value=None, ge_value=None):
    # TODO(ajamato): Consider adding a matcher framework
    found = 0
    for mi in monitoring_infos:
      if has_urn_and_labels(mi, urn, labels):
        if ge_value is not None:
          if mi.metric.counter_data.int64_value >= ge_value:
            found = found + 1
        elif value is not None:
          if mi.metric.counter_data.int64_value == value:
            found = found + 1
        else:
          found = found + 1
    ge_value_str = {'ge_value' : ge_value} if ge_value else ''
    value_str = {'value' : value} if value else ''
    self.assertEqual(
        1, found, "Found (%s) Expected only 1 monitoring_info for %s." %
        (found, (urn, labels, value_str, ge_value_str),))

  def assert_has_distribution(
      self, monitoring_infos, urn, labels,
      sum=None, count=None, min=None, max=None):
    # TODO(ajamato): Consider adding a matcher framework
    sum = _matcher_or_equal_to(sum)
    count = _matcher_or_equal_to(count)
    min = _matcher_or_equal_to(min)
    max = _matcher_or_equal_to(max)
    found = 0
    description = StringDescription()
    for mi in monitoring_infos:
      if has_urn_and_labels(mi, urn, labels):
        int_dist = mi.metric.distribution_data.int_distribution_data
        increment = 1
        if sum is not None:
          description.append_text(' sum: ')
          sum.describe_to(description)
          if not sum.matches(int_dist.sum):
            increment = 0
        if count is not None:
          description.append_text(' count: ')
          count.describe_to(description)
          if not count.matches(int_dist.count):
            increment = 0
        if min is not None:
          description.append_text(' min: ')
          min.describe_to(description)
          if not min.matches(int_dist.min):
            increment = 0
        if max is not None:
          description.append_text(' max: ')
          max.describe_to(description)
          if not max.matches(int_dist.max):
            increment = 0
        found += increment
    self.assertEqual(
        1, found, "Found (%s) Expected only 1 monitoring_info for %s." %
        (found, (urn, labels, str(description)),))

  def create_pipeline(self):
    p = beam.Pipeline(runner=fn_api_runner.FnApiRunner())
    # TODO(BEAM-8448): Fix these tests.
    p.options.view_as(DebugOptions).experiments.remove('beam_fn_api')
    return p

  def test_element_count_metrics(self):
    class GenerateTwoOutputs(beam.DoFn):
      def process(self, element):
        yield str(element) + '1'
        yield beam.pvalue.TaggedOutput('SecondOutput', str(element) + '2')
        yield beam.pvalue.TaggedOutput('SecondOutput', str(element) + '2')
        yield beam.pvalue.TaggedOutput('ThirdOutput', str(element) + '3')

    class PassThrough(beam.DoFn):
      def process(self, element):
        yield element

    p = self.create_pipeline()
    if not isinstance(p.runner, fn_api_runner.FnApiRunner):
      # This test is inherited by others that may not support the same
      # internal way of accessing progress metrics.
      self.skipTest('Metrics not supported.')

    # Produce enough elements to make sure byte sampling occurs.
    num_source_elems = 100
    pcoll = p | beam.Create(['a%d' % i for i in range(num_source_elems)])

    # pylint: disable=expression-not-assigned
    pardo = ('StepThatDoesTwoOutputs' >> beam.ParDo(
        GenerateTwoOutputs()).with_outputs('SecondOutput',
                                           'ThirdOutput',
                                           main='FirstAndMainOutput'))

    # Actually feed pcollection to pardo
    second_output, third_output, first_output = (pcoll | pardo)

    # consume some of elements
    merged = ((first_output, second_output, third_output) | beam.Flatten())
    merged | ('PassThrough') >> beam.ParDo(PassThrough())
    second_output | ('PassThrough2') >> beam.ParDo(PassThrough())

    res = p.run()
    res.wait_until_finish()

    result_metrics = res.monitoring_metrics()

    counters = result_metrics.monitoring_infos()
    # All element count and byte count metrics must have a PCOLLECTION_LABEL.
    self.assertFalse([x for x in counters if
                      x.urn in [monitoring_infos.ELEMENT_COUNT_URN,
                                monitoring_infos.SAMPLED_BYTE_SIZE_URN]
                      and
                      monitoring_infos.PCOLLECTION_LABEL not in x.labels])
    try:
      labels = {monitoring_infos.PCOLLECTION_LABEL : 'Impulse'}
      self.assert_has_counter(
          counters, monitoring_infos.ELEMENT_COUNT_URN, labels, 1)

      # Create/Read, "out" output.
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_1'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))

      # GenerateTwoOutputs, main output.
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_2'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))

      # GenerateTwoOutputs, "SecondOutput" output.
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_3'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, 2 * num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))

      # GenerateTwoOutputs, "ThirdOutput" output.
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_4'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))

      # Skipping other pcollections due to non-deterministic naming for multiple
      # outputs.
      # Flatten/Read, main output.
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_5'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, 4 * num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))

      # PassThrough, main output
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_6'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, 4 * num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))

      # PassThrough2, main output
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                    'ref_PCollection_PCollection_7'}
      self.assert_has_counter(
          counters,
          monitoring_infos.ELEMENT_COUNT_URN, labels, num_source_elems)
      self.assert_has_distribution(
          counters, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels,
          min=hamcrest.greater_than(0),
          max=hamcrest.greater_than(0),
          sum=hamcrest.greater_than(0),
          count=hamcrest.greater_than(0))
    except:
      print(res._monitoring_infos_by_stage)
      raise

  def test_non_user_metrics(self):
    p = self.create_pipeline()
    if not isinstance(p.runner, fn_api_runner.FnApiRunner):
      # This test is inherited by others that may not support the same
      # internal way of accessing progress metrics.
      self.skipTest('Metrics not supported.')

    pcoll = p | beam.Create(['a', 'zzz'])
    # pylint: disable=expression-not-assigned
    pcoll | 'MyStep' >> beam.FlatMap(lambda x: None)
    res = p.run()
    res.wait_until_finish()

    result_metrics = res.monitoring_metrics()
    all_metrics_via_montoring_infos = result_metrics.query()

    def assert_counter_exists(metrics, namespace, name, step):
      found = 0
      metric_key = MetricKey(step, MetricName(namespace, name))
      for m in metrics['counters']:
        if m.key == metric_key:
          found = found + 1
      self.assertEqual(
          1, found, "Did not find exactly 1 metric for %s." % metric_key)
    urns = [
        monitoring_infos.START_BUNDLE_MSECS_URN,
        monitoring_infos.PROCESS_BUNDLE_MSECS_URN,
        monitoring_infos.FINISH_BUNDLE_MSECS_URN,
        monitoring_infos.TOTAL_MSECS_URN,
    ]
    for urn in urns:
      split = urn.split(':')
      namespace = split[0]
      name = ':'.join(split[1:])
      assert_counter_exists(
          all_metrics_via_montoring_infos, namespace, name, step='Create/Read')
      assert_counter_exists(
          all_metrics_via_montoring_infos, namespace, name, step='MyStep')

  # Due to somewhat non-deterministic nature of state sampling and sleep,
  # this test is flaky when state duration is low.
  # Since increasing state duration significantly would also slow down
  # the test suite, we are retrying twice on failure as a mitigation.
  @retry(reraise=True, stop=stop_after_attempt(3))
  def test_progress_metrics(self):
    p = self.create_pipeline()
    if not isinstance(p.runner, fn_api_runner.FnApiRunner):
      # This test is inherited by others that may not support the same
      # internal way of accessing progress metrics.
      self.skipTest('Progress metrics not supported.')
      return

    _ = (p
         | beam.Create([0, 0, 0, 5e-3 * DEFAULT_SAMPLING_PERIOD_MS])
         | beam.Map(time.sleep)
         | beam.Map(lambda x: ('key', x))
         | beam.GroupByKey()
         | 'm_out' >> beam.FlatMap(lambda x: [
             1, 2, 3, 4, 5,
             beam.pvalue.TaggedOutput('once', x),
             beam.pvalue.TaggedOutput('twice', x),
             beam.pvalue.TaggedOutput('twice', x)]))

    res = p.run()
    res.wait_until_finish()

    def has_mi_for_ptransform(mon_infos, ptransform):
      for mi in mon_infos:
        if ptransform in mi.labels[monitoring_infos.PTRANSFORM_LABEL]:
          return True
      return False

    try:
      # TODO(ajamato): Delete this block after deleting the legacy metrics code.
      # Test the DEPRECATED legacy metrics
      pregbk_metrics, postgbk_metrics = list(
          res._metrics_by_stage.values())
      if 'Create/Read' not in pregbk_metrics.ptransforms:
        # The metrics above are actually unordered. Swap.
        pregbk_metrics, postgbk_metrics = postgbk_metrics, pregbk_metrics
      self.assertEqual(
          4,
          pregbk_metrics.ptransforms['Create/Read']
          .processed_elements.measured.output_element_counts['out'])
      self.assertEqual(
          4,
          pregbk_metrics.ptransforms['Map(sleep)']
          .processed_elements.measured.output_element_counts['None'])
      self.assertLessEqual(
          4e-3 * DEFAULT_SAMPLING_PERIOD_MS,
          pregbk_metrics.ptransforms['Map(sleep)']
          .processed_elements.measured.total_time_spent)
      self.assertEqual(
          1,
          postgbk_metrics.ptransforms['GroupByKey/Read']
          .processed_elements.measured.output_element_counts['None'])

      # The actual stage name ends up being something like 'm_out/lamdbda...'
      m_out, = [
          metrics for name, metrics in list(postgbk_metrics.ptransforms.items())
          if name.startswith('m_out')]
      self.assertEqual(
          5,
          m_out.processed_elements.measured.output_element_counts['None'])
      self.assertEqual(
          1,
          m_out.processed_elements.measured.output_element_counts['once'])
      self.assertEqual(
          2,
          m_out.processed_elements.measured.output_element_counts['twice'])

      # Test the new MonitoringInfo monitoring format.
      self.assertEqual(2, len(res._monitoring_infos_by_stage))
      pregbk_mis, postgbk_mis = list(res._monitoring_infos_by_stage.values())

      if not has_mi_for_ptransform(pregbk_mis, 'Create/Read'):
        # The monitoring infos above are actually unordered. Swap.
        pregbk_mis, postgbk_mis = postgbk_mis, pregbk_mis

      # pregbk monitoring infos
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                'ref_PCollection_PCollection_1'}
      self.assert_has_counter(
          pregbk_mis, monitoring_infos.ELEMENT_COUNT_URN, labels, value=4)
      self.assert_has_distribution(
          pregbk_mis, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels)

      labels = {monitoring_infos.PCOLLECTION_LABEL :
                'ref_PCollection_PCollection_2'}
      self.assert_has_counter(
          pregbk_mis, monitoring_infos.ELEMENT_COUNT_URN, labels, value=4)
      self.assert_has_distribution(
          pregbk_mis, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels)

      labels = {monitoring_infos.PTRANSFORM_LABEL : 'Map(sleep)'}
      self.assert_has_counter(
          pregbk_mis, monitoring_infos.TOTAL_MSECS_URN,
          labels, ge_value=4 * DEFAULT_SAMPLING_PERIOD_MS)

      # postgbk monitoring infos
      labels = {monitoring_infos.PCOLLECTION_LABEL :
                'ref_PCollection_PCollection_6'}
      self.assert_has_counter(
          postgbk_mis, monitoring_infos.ELEMENT_COUNT_URN, labels, value=1)
      self.assert_has_distribution(
          postgbk_mis, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels)

      labels = {monitoring_infos.PCOLLECTION_LABEL :
                'ref_PCollection_PCollection_7'}
      self.assert_has_counter(
          postgbk_mis, monitoring_infos.ELEMENT_COUNT_URN, labels, value=5)
      self.assert_has_distribution(
          postgbk_mis, monitoring_infos.SAMPLED_BYTE_SIZE_URN, labels)
    except:
      print(res._monitoring_infos_by_stage)
      raise


class FnApiRunnerTestWithGrpc(FnApiRunnerTest):

  def create_pipeline(self):
    return beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC)))


class FnApiRunnerTestWithGrpcMultiThreaded(FnApiRunnerTest):

  def create_pipeline(self):
    return beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC,
                payload=b'2,%d' % fn_api_runner.STATE_CACHE_SIZE)))


class FnApiRunnerTestWithDisabledCaching(FnApiRunnerTest):

  def create_pipeline(self):
    return beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC,
                # number of workers, state cache size
                payload=b'2,0')))


class FnApiRunnerTestWithMultiWorkers(FnApiRunnerTest):

  def create_pipeline(self):
    pipeline_options = PipelineOptions(direct_num_workers=2)
    p = beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(),
        options=pipeline_options)
    #TODO(BEAM-8444): Fix these tests..
    p.options.view_as(DebugOptions).experiments.remove('beam_fn_api')
    return p

  def test_metrics(self):
    raise unittest.SkipTest("This test is for a single worker only.")

  def test_sdf_with_sdf_initiated_checkpointing(self):
    raise unittest.SkipTest("This test is for a single worker only.")


class FnApiRunnerTestWithGrpcAndMultiWorkers(FnApiRunnerTest):

  def create_pipeline(self):
    pipeline_options = PipelineOptions(direct_num_workers=2)
    p = beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC)),
        options=pipeline_options)
    #TODO(BEAM-8444): Fix these tests..
    p.options.view_as(DebugOptions).experiments.remove('beam_fn_api')
    return p

  def test_metrics(self):
    raise unittest.SkipTest("This test is for a single worker only.")

  def test_sdf_with_sdf_initiated_checkpointing(self):
    raise unittest.SkipTest("This test is for a single worker only.")


class FnApiRunnerTestWithBundleRepeat(FnApiRunnerTest):

  def create_pipeline(self):
    return beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(bundle_repeat=3))

  def test_register_finalizations(self):
    raise unittest.SkipTest("TODO: Avoid bundle finalizations on repeat.")


class FnApiRunnerTestWithBundleRepeatAndMultiWorkers(FnApiRunnerTest):

  def create_pipeline(self):
    pipeline_options = PipelineOptions(direct_num_workers=2)
    p = beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(bundle_repeat=3),
        options=pipeline_options)
    p.options.view_as(DebugOptions).experiments.remove('beam_fn_api')
    return p

  def test_register_finalizations(self):
    raise unittest.SkipTest("TODO: Avoid bundle finalizations on repeat.")

  def test_metrics(self):
    raise unittest.SkipTest("This test is for a single worker only.")

  def test_sdf_with_sdf_initiated_checkpointing(self):
    raise unittest.SkipTest("This test is for a single worker only.")


class FnApiRunnerSplitTest(unittest.TestCase):

  def create_pipeline(self):
    # Must be GRPC so we can send data and split requests concurrent
    # to the bundle process request.
    return beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC)))

  def test_checkpoint(self):
    # This split manager will get re-invoked on each smaller split,
    # so N times for N elements.
    element_counter = ElementCounter()

    def split_manager(num_elements):
      # Send at least one element so it can make forward progress.
      element_counter.reset()
      breakpoint = element_counter.set_breakpoint(1)
      # Cede control back to the runner so data can be sent.
      yield
      breakpoint.wait()
      # Split as close to current as possible.
      split_result = yield 0.0
      # Verify we split at exactly the first element.
      self.verify_channel_split(split_result, 0, 1)
      # Continue processing.
      breakpoint.clear()

    self.run_split_pipeline(split_manager, list('abc'), element_counter)

  def test_split_half(self):
    total_num_elements = 25
    seen_bundle_sizes = []
    element_counter = ElementCounter()

    def split_manager(num_elements):
      seen_bundle_sizes.append(num_elements)
      if num_elements == total_num_elements:
        element_counter.reset()
        breakpoint = element_counter.set_breakpoint(5)
        yield
        breakpoint.wait()
        # Split the remainder (20, then 10, elements) in half.
        split1 = yield 0.5
        self.verify_channel_split(split1, 14, 15)  # remainder is 15 to end
        split2 = yield 0.5
        self.verify_channel_split(split2, 9, 10)   # remainder is 10 to end
        breakpoint.clear()

    self.run_split_pipeline(
        split_manager, range(total_num_elements), element_counter)
    self.assertEqual([25, 15], seen_bundle_sizes)

  def run_split_pipeline(self, split_manager, elements, element_counter=None):
    with fn_api_runner.split_manager('Identity', split_manager):
      with self.create_pipeline() as p:
        res = (p
               | beam.Create(elements)
               | beam.Reshuffle()
               | 'Identity' >> beam.Map(lambda x: x)
               | beam.Map(lambda x: element_counter.increment() or x))
        assert_that(res, equal_to(elements))

  def test_nosplit_sdf(self):
    def split_manager(num_elements):
      yield

    elements = [1, 2, 3]
    expected_groups = [[(e, k) for k in range(e)] for e in elements]
    self.run_sdf_split_pipeline(
        split_manager, elements, ElementCounter(), expected_groups)

  def test_checkpoint_sdf(self):
    element_counter = ElementCounter()

    def split_manager(num_elements):
      if num_elements > 0:
        element_counter.reset()
        breakpoint = element_counter.set_breakpoint(1)
        yield
        breakpoint.wait()
        yield 0
        breakpoint.clear()

    # Everything should be perfectly split.
    elements = [2, 3]
    expected_groups = [[(2, 0)], [(2, 1)], [(3, 0)], [(3, 1)], [(3, 2)]]
    self.run_sdf_split_pipeline(
        split_manager, elements, element_counter, expected_groups)

  def test_split_half_sdf(self):

    element_counter = ElementCounter()
    is_first_bundle = [True]  # emulate nonlocal for Python 2

    def split_manager(num_elements):
      if is_first_bundle and num_elements > 0:
        del is_first_bundle[:]
        breakpoint = element_counter.set_breakpoint(1)
        yield
        breakpoint.wait()
        split1 = yield 0.5
        split2 = yield 0.5
        split3 = yield 0.5
        self.verify_channel_split(split1, 0, 1)
        self.verify_channel_split(split2, -1, 1)
        self.verify_channel_split(split3, -1, 1)
        breakpoint.clear()

    elements = [4, 4]
    expected_groups = [
        [(4, 0)],
        [(4, 1)],
        [(4, 2), (4, 3)],
        [(4, 0), (4, 1), (4, 2), (4, 3)]]

    self.run_sdf_split_pipeline(
        split_manager, elements, element_counter, expected_groups)

  def test_split_crazy_sdf(self, seed=None):
    if seed is None:
      seed = random.randrange(1 << 20)
    r = random.Random(seed)
    element_counter = ElementCounter()

    def split_manager(num_elements):
      if num_elements > 0:
        element_counter.reset()
        wait_for = r.randrange(num_elements)
        breakpoint = element_counter.set_breakpoint(wait_for)
        yield
        breakpoint.wait()
        yield r.random()
        yield r.random()
        breakpoint.clear()

    try:
      elements = [r.randrange(5, 10) for _ in range(5)]
      self.run_sdf_split_pipeline(split_manager, elements, element_counter)
    except Exception:
      logging.error('test_split_crazy_sdf.seed = %s', seed)
      raise

  def run_sdf_split_pipeline(
      self, split_manager, elements, element_counter, expected_groups=None):
    # Define an SDF that for each input x produces [(x, k) for k in range(x)].

    class EnumerateProvider(beam.transforms.core.RestrictionProvider):
      def initial_restriction(self, element):
        return restriction_trackers.OffsetRange(0, element)

      def create_tracker(self, restriction):
        return restriction_trackers.OffsetRestrictionTracker(restriction)

      def split(self, element, restriction):
        # Don't do any initial splitting to simplify test.
        return [restriction]

      def restriction_size(self, element, restriction):
        return restriction.size()

    class EnumerateSdf(beam.DoFn):
      def process(
          self,
          element,
          restriction_tracker=beam.DoFn.RestrictionParam(EnumerateProvider())):
        to_emit = []
        cur = restriction_tracker.start_position()
        while restriction_tracker.try_claim(cur):
          to_emit.append((element, cur))
          element_counter.increment()
          cur += 1
        # Emitting in batches for tighter testing.
        yield to_emit

    expected = [(e, k) for e in elements for k in range(e)]

    with fn_api_runner.split_manager('SDF', split_manager):
      with self.create_pipeline() as p:
        grouped = (
            p
            | beam.Create(elements)
            | 'SDF' >> beam.ParDo(EnumerateSdf()))
        flat = grouped | beam.FlatMap(lambda x: x)
        assert_that(flat, equal_to(expected))
        if expected_groups:
          assert_that(grouped, equal_to(expected_groups), label='CheckGrouped')

  def verify_channel_split(self, split_result, last_primary, first_residual):
    self.assertEqual(1, len(split_result.channel_splits), split_result)
    channel_split, = split_result.channel_splits
    self.assertEqual(last_primary, channel_split.last_primary_element)
    self.assertEqual(first_residual, channel_split.first_residual_element)
    # There should be a primary and residual application for each element
    # not covered above.
    self.assertEqual(
        first_residual - last_primary - 1,
        len(split_result.primary_roots),
        split_result.primary_roots)
    self.assertEqual(
        first_residual - last_primary - 1,
        len(split_result.residual_roots),
        split_result.residual_roots)


class ElementCounter(object):
  """Used to wait until a certain number of elements are seen."""

  def __init__(self):
    self._cv = threading.Condition()
    self.reset()

  def reset(self):
    with self._cv:
      self._breakpoints = collections.defaultdict(list)
      self._count = 0

  def increment(self):
    with self._cv:
      self._count += 1
      self._cv.notify_all()
      breakpoints = list(self._breakpoints[self._count])
    for breakpoint in breakpoints:
      breakpoint.wait()

  def set_breakpoint(self, value):
    with self._cv:
      event = threading.Event()
      self._breakpoints[value].append(event)

    class Breakpoint(object):
      @staticmethod
      def wait(timeout=10):
        with self._cv:
          start = time.time()
          while self._count < value:
            elapsed = time.time() - start
            if elapsed > timeout:
              raise RuntimeError('Timed out waiting for %s' % value)
            self._cv.wait(timeout - elapsed)

      @staticmethod
      def clear():
        event.set()

    return Breakpoint()

  def __reduce__(self):
    # Ensure we get the same element back through a pickling round-trip.
    name = uuid.uuid4().hex
    _pickled_element_counters[name] = self
    return _unpickle_element_counter, (name,)


_pickled_element_counters = {}


def _unpickle_element_counter(name):
  return _pickled_element_counters[name]


class EventRecorder(object):
  """Used to be registered as a callback in bundle finalization.

  The reason why records are written into a tmp file is, the in-memory dataset
  cannot keep callback records when passing into one DoFn.
  """
  def __init__(self, tmp_dir):
    self.tmp_dir = os.path.join(tmp_dir, uuid.uuid4().hex)
    os.mkdir(self.tmp_dir)

  def record(self, content):
    file_path = os.path.join(self.tmp_dir, uuid.uuid4().hex + '.txt')
    with open(file_path, 'w') as f:
      f.write(content)

  def events(self):
    content = []
    record_files = [f for f in os.listdir(self.tmp_dir) if os.path.isfile(
        os.path.join(self.tmp_dir, f))]
    for file in record_files:
      with open(os.path.join(self.tmp_dir, file), 'r') as f:
        content.append(f.read())
    return sorted(content)

  def cleanup(self):
    shutil.rmtree(self.tmp_dir)


class ExpandStringsProvider(beam.transforms.core.RestrictionProvider):
  """A RestrictionProvider that used for sdf related tests."""
  def initial_restriction(self, element):
    return restriction_trackers.OffsetRange(0, len(element))

  def create_tracker(self, restriction):
    return restriction_trackers.OffsetRestrictionTracker(restriction)

  def split(self, element, restriction):
    desired_bundle_size = restriction.size() // 2
    return restriction.split(desired_bundle_size)

  def restriction_size(self, element, restriction):
    return restriction.size()


class FnApiRunnerSplitTestWithMultiWorkers(FnApiRunnerSplitTest):

  def create_pipeline(self):
    pipeline_options = PipelineOptions(direct_num_workers=2)
    p = beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC)),
        options=pipeline_options)
    #TODO(BEAM-8444): Fix these tests..
    p.options.view_as(DebugOptions).experiments.remove('beam_fn_api')
    return p

  def test_checkpoint(self):
    raise unittest.SkipTest("This test is for a single worker only.")

  def test_split_half(self):
    raise unittest.SkipTest("This test is for a single worker only.")


class FnApiBasedLullLoggingTest(unittest.TestCase):
  def create_pipeline(self):
    return beam.Pipeline(
        runner=fn_api_runner.FnApiRunner(
            default_environment=beam_runner_api_pb2.Environment(
                urn=python_urns.EMBEDDED_PYTHON_GRPC),
            progress_request_frequency=0.5))

  def test_lull_logging(self):

    # TODO(BEAM-1251): Remove this test skip after dropping Py 2 support.
    if sys.version_info < (3, 4):
      self.skipTest('Log-based assertions are supported after Python 3.4')
    try:
      utils.check_compiled('apache_beam.runners.worker.opcounters')
    except RuntimeError:
      self.skipTest('Cython is not available')

    with self.assertLogs(level='WARNING') as logs:
      with self.create_pipeline() as p:
        sdk_worker.DEFAULT_LOG_LULL_TIMEOUT_NS = 1000 * 1000  # Lull after 1 ms

        _ = (p
             | beam.Create([1])
             | beam.Map(time.sleep))

    self.assertRegex(
        ''.join(logs.output),
        '.*There has been a processing lull of over.*',
        'Unable to find a lull logged for this job.')


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()
