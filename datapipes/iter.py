import time
import types

from torch.utils.data import IterDataPipe, IterableDataset, functional_datapipe, non_deterministic

import datapipes
import datapipes.nonblocking as nonblocking

DEFAULT_NON_BLOCKING_SLEEP = 0.001


def default_not_available_hook():
    time.sleep(DEFAULT_NON_BLOCKING_SLEEP)


class NonBlocking(IterDataPipe):
    not_available_hook = default_not_available_hook

    def __iter__(self):
        self.reset_iterator()
        return self

    def __next__(self):
        while True:
            try:
                return self.nonblocking_next()
            except StopIteration:
                raise StopIteration
            except nonblocking.NotAvailable:
                if NonBlocking.not_available_hook is not None:
                    NonBlocking.not_available_hook()

    def nonblocking_next(self):
        raise NotImplementedError(
            "nonblocking_next is not implemented for %s" % self.__class__)

    def reset_iterator(self):
        raise NotImplementedError(
            "reset_iterator is not implemented for %s" % self.__class__)

    @staticmethod
    def register_not_available_hook(hook_function):
        NonBlocking.not_available_hook = hook_function


class IterDatasetWrapper(IterDataPipe):
    def __init__(self, iterdataset):
        if not isinstance(iterdataset, IterableDataset):
            raise Exception('Not IteratableDataset')
        self.ds = iterdataset

    def __iter__(self):
        for i in self.ds:
            yield i


def EnsureNonBlockingDataPipe(validated_datapipe):
    if not isinstance(validated_datapipe, IterDataPipe):
        raise Exception('Not Iterable DataPipe ' +
                        str(validated_datapipe.__class__))
    if isinstance(validated_datapipe, NonBlocking):
        return validated_datapipe
    if not hasattr(validated_datapipe, '_as_iterator'):
        setattr(validated_datapipe, '_as_iterator', None)
    if not hasattr(validated_datapipe, 'nonblocking_next'):
        def nonblocking_next(self):
            if self._as_iterator is None:
                self._as_iterator = iter(self)
            return next(self._as_iterator)
        setattr(validated_datapipe, 'nonblocking_next', nonblocking_next)
        validated_datapipe.nonblocking_next = types.MethodType(
            nonblocking_next, validated_datapipe)
    if not hasattr(validated_datapipe, 'reset_iterator'):
        def reset_iterator(self):
            self._as_iterator = None
        setattr(validated_datapipe, 'reset_iterator', reset_iterator)
        validated_datapipe.reset_iterator = types.MethodType(
            reset_iterator, validated_datapipe)
    return validated_datapipe


@functional_datapipe('join')
@non_deterministic(lambda *args: len(args) > 1)
class GreedyJoin(NonBlocking):
    def __init__(self, *datapipes):
        self.datapipes = [
            EnsureNonBlockingDataPipe(dp) for dp in datapipes]
        self.exclude_datapipes = []

    def reset_iterator(self):
        self.exclude_datapipes = []
        for dp in self.datapipes:
            dp.reset_iterator()

    def nonblocking_next(self):
        not_available = False
        for dp in self.datapipes:
            if dp not in self.exclude_datapipes:
                try:
                    value = dp.nonblocking_next()
                    return value
                except StopIteration:
                    self.exclude_datapipes.append(dp)
                    pass
                except nonblocking.NotAvailable:
                    not_available = True
        if not_available:
            raise nonblocking.NotAvailable
        else:
            raise StopIteration


# Not real prefetcher, used only as reference, need to be replaced with the queues implementation
class Prefetcher(NonBlocking):
    def __init__(self, source_dp, buffer_size=10):
        self._source_dp = EnsureNonBlockingDataPipe(source_dp)
        self._buffer_size = buffer_size
        self._buffer = []
        self._source_depleted = False

    def nonblocking_next(self):
        if not self._source_depleted:
            while len(self._buffer) < self._buffer_size:
                try:
                    data = self._source_dp.nonblocking_next()
                except nonblocking.NotAvailable:
                    # break or put more requests, depends from implementation
                    break
                except StopIteration:
                    self._source_depleted = True
                    break
                self._buffer.append(data)
        if len(self._buffer):
            data = self._buffer.pop(0)
            return data
        else:
            if self._source_depleted:
                raise StopIteration
            else:
                raise nonblocking.NotAvailable


class MultipliedIterDataPipe(NonBlocking):
    def __init__(self, multiplier, pipe_id):
        self._multiplier = multiplier
        self._id = pipe_id

    def nonblocking_next(self):
        return self._multiplier.nonblocking_next_mult(self._id)

    def reset_iterator(self):
        self._multiplier.reset_iterator(self._id)


# Implementation with one element buffer
class _Multiply():
    def __init__(self, source_dp, instances):
        self._source_dp = source_dp
        self._instances = instances
        self._reset_vars()

    def _reset_vars(self):
        self._reset_calls = {}
        self._stop_iteration = False
        self._data = {}

    def nonblocking_next_mult(self, pipe_id):
        # If ANY of my pipes requested reset that means all pipes should request reset
        if len(self._reset_calls.keys()) > 0:
            raise nonblocking.NotAvailable

        if not self._data.keys():
            # if one of the pipes got StopIteration other pipes should get it too
            if self._stop_iteration:
                raise StopIteration
            try:
                value = self._source_dp.nonblocking_next()
            except StopIteration:
                self._stop_iteration = True
                raise StopIteration
            except nonblocking.NotAvailable:
                raise nonblocking.NotAvailable
            self._data = {i: value for i in range(self._instances)}
        if pipe_id in self._data:
            value = self._data[pipe_id]
            del self._data[pipe_id]
            return value
        else:
            raise nonblocking.NotAvailable

    def reset_iterator(self, pipe_id):
        # Only reset after all pipes agreed to reset
        self._reset_calls[pipe_id] = True
        if len(self._reset_calls.keys()) == self._instances:
            self._source_dp.reset_iterator()
            self._reset_vars()


class Multiply():
    def __new__(cls, source_dp, instances):
        source_dp = EnsureNonBlockingDataPipe(source_dp)
        connector = _Multiply(source_dp, instances)
        return [MultipliedIterDataPipe(connector, i) for i in range(instances)]

    def __init__(self, *arg):
        raise Exception('__init__ called instead of __new__')


class RoutedIterDataPipe(NonBlocking):
    def __init__(self, router, pipe_id):
        self._router = router
        self._id = pipe_id

    def nonblocking_next(self):
        return self._router.nonblocking_next_mult(self._id)

    def reset_iterator(self):
        self._router.reset_iterator(self._id)

# Implementation with one element buffer


class _Router():
    def __init__(self, source_dp, priority_fns):
        self._source_dp = source_dp
        self._priority_fns = priority_fns
        self._instances = len(priority_fns)
        self._stop_iteration = False
        self._next_item = None
        self._reset_calls = {}
        self._get_guards = {}

    def nonblocking_next_mult(self, pipe_id):
        # If ANY of my pipes requested reset that means all pipes should request reset
        if len(self._reset_calls.keys()) > 0:
            raise nonblocking.NotAvailable

        if self._next_item is None:
            # if one of the pipes got StopIteration other pipes should get it too
            if self._stop_iteration:
                raise StopIteration
            try:
                value = self._source_dp.nonblocking_next()
            except StopIteration:
                self._stop_iteration = True
                raise StopIteration
            except nonblocking.NotAvailable:
                raise nonblocking.NotAvailable
            self._next_item = value
            self._get_guards = {}
        value = self._next_item

        if self._priority_fns[pipe_id](value):
            self._next_item = None
            return value
        else:
            self._get_guards[pipe_id] = True
            if len(self._get_guards.keys()) == self._instances:
                raise Exception("None of the priority functions satisfy input data", value)
            raise nonblocking.NotAvailable

    def reset_iterator(self, pipe_id):
        # Only reset after all pipes agreed to reset
        self._reset_calls[pipe_id] = True
        if len(self._reset_calls.keys()) == self._instances:
            self._source_dp.reset_iterator()
            self._reset_calls = {}
            self._stop_iteration = False
            self._next_item = None
            self._get_guards = {}


class Router():
    def __new__(cls, source_dp, priority_fns):
        source_dp = EnsureNonBlockingDataPipe(source_dp)
        connector = _Router(source_dp, priority_fns)
        return [RoutedIterDataPipe(connector, i) for i in range(len(priority_fns))]

    def __init__(self, *arg):
        raise Exception('__init__ called instead of __new__')


# Creates iter.DataPipe which reads data from the DataLoader.Queue
class QueueWrapper(NonBlocking):
    def __init__(self, request_queue, response_queue, response_wait_time=0.00001):
        self._req_q = request_queue
        self._res_q = response_queue
        self._req_sent = False
        self.counter = 0
        self._stop_iteration = False
        self._response_wait_time = response_wait_time

    def reset_iterator(self):
        if self._req_sent:
            raise Exception(
                'Can not reset QueueWrapper while it is still waiting for response for', self._req_q.name)
        self._stop_iteration = False
        self.counter = 0
        self._req_q.put(datapipes.nonblocking.ResetIteratorRequest())
        while True:
            try:
                value = self._res_q.get(block=False)
                break
            except:
                if NonBlocking.not_available_hook is not None:
                    NonBlocking.not_available_hook()

        if not isinstance(value, datapipes.nonblocking.ResetIteratorResponse):
            raise Exception('Invalid response received')

    def nonblocking_next(self):
        if self._stop_iteration:
            raise Exception(
                '`next` or `nonblocking_next` called after receiving StopIteration')
        if not self._req_sent:
            self._req_q.put(self.counter)
            self.counter += 1
            self._req_sent = True

            # return control to eventloop to fill results if possible
            # eventloop.EventLoop.iteration()
            # return control as previous solution ends with infinite loop
            # can be only used in case of event loop
            # raise datapipes.nonblocking.NotAvailable

        try:
            value = self._res_q.get(
                block=True, timeout=self._response_wait_time)
        except:  # TODO: Catch only timeout exceptions
            raise nonblocking.NotAvailable
        self._req_sent = False
        if isinstance(value, StopIteration):
            self._stop_iteration = True
            raise StopIteration
        return value

# Indefinitely iterates over req_queue and passing values from source_datapipe to res_queue
# If raise_stop is true, raises exception when StopIteration received from the source_datapipe
def DataPipeBehindQueues(source_datapipe, req_queue, res_queue, full_stop=False, nonblocking_next_function_name='nonblocking_next', blocking_request_get=False):
    source_datapipe = datapipes.iter.EnsureNonBlockingDataPipe(
        source_datapipe)
    forever = True
    while forever:
        try:
            # Non-blocking call is Extremely slow here for python.mp, need to figureout good workaround
            request = req_queue.get(block=blocking_request_get)
        except:
            yield True
            continue

        if isinstance(request, datapipes.nonblocking.ResetIteratorRequest):
            source_datapipe.reset_iterator()
            res_queue.put(datapipes.nonblocking.ResetIteratorResponse())
            continue

        if isinstance(request, datapipes.nonblocking.StopIteratorRequest):
            forever = False
            res_queue.put(datapipes.nonblocking.StopIteratorResponse())
            continue

        while forever:
            try:
                function = getattr(
                    source_datapipe, nonblocking_next_function_name)
                value = function()
            except datapipes.nonblocking.NotAvailable:
                yield True
                continue
            except StopIteration:
                res_queue.put(StopIteration())
                if full_stop:
                    forever = False
                else:
                    yield True
                break
            res_queue.put(value, block=True)
            yield True  # Returns control
            break



# Must sit on top of non-shardable deterministic datapipe to skip some items
class SimpleSharding(IterDataPipe):
    def __init__(self, source_datapipe):
        self.source_datapipe = source_datapipe
        self.num_shards = 1
        self.shard_id = 0

    def is_shardable(self):
        return True

    def sharding_settings(self, num_shards, shard_id):
        self.num_shards = num_shards
        self.shard_id = shard_id

    def __iter__(self):
        for i, item in enumerate(self.source_datapipe):
            if i % self.num_shards == self.shard_id:
                yield item
