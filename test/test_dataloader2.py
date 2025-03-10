# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import pickle
import unittest
from unittest import TestCase

from torchdata.dataloader2 import DataLoader2, MultiProcessingReadingService, ReadingServiceInterface
from torchdata.dataloader2.dataloader2 import READING_SERVICE_STATE_KEY_NAME, SERIALIZED_DATAPIPE_KEY_NAME
from torchdata.datapipes.iter import IterableWrapper, IterDataPipe


class TestReadingService(ReadingServiceInterface):
    def initialize(self, dp: IterDataPipe) -> IterDataPipe:
        return dp


class DataLoader2Test(TestCase):
    def test_dataloader2(self) -> None:
        test_data_pipe = IterableWrapper(range(3))
        data_loader = DataLoader2(datapipe=test_data_pipe)

        expected_batch = 0
        for batch in iter(data_loader):
            self.assertEqual(batch, expected_batch)
            expected_batch += 1

    def test_dataloader2_shutdown(self) -> None:
        test_data_pipe = IterableWrapper(range(3))
        data_loader = DataLoader2(datapipe=test_data_pipe)
        data_loader.shutdown()

    def test_dataloader2_state_dict(self) -> None:
        test_data_pipe = IterableWrapper(range(3))
        data_loader = DataLoader2(datapipe=test_data_pipe)

        state = data_loader.state_dict()
        self.assertIsNotNone(state)
        self.assertIsNotNone(state[SERIALIZED_DATAPIPE_KEY_NAME])
        self.assertIsNone(state[READING_SERVICE_STATE_KEY_NAME])
        data_loader.shutdown()

    def test_dataloader2_reading_service(self) -> None:
        test_data_pipe = IterableWrapper(range(3))
        reading_service = TestReadingService()
        data_loader = DataLoader2(datapipe=test_data_pipe, reading_service=reading_service)

        expected_batch = 0
        for batch in iter(data_loader):
            self.assertEqual(batch, expected_batch)
            expected_batch += 1

    def test_dataloader2_multi_process_reading_service(self) -> None:
        test_data_pipe = IterableWrapper(range(3))
        reading_service = MultiProcessingReadingService()
        data_loader = DataLoader2(datapipe=test_data_pipe, reading_service=reading_service)

        expected_batch = 0
        for batch in iter(data_loader):
            self.assertEqual(batch, expected_batch)
            expected_batch += 1

    def test_dataloader2_load_state_dict(self) -> None:
        test_data_pipe = IterableWrapper(range(3))
        reading_service = TestReadingService()
        data_loader = DataLoader2(datapipe=test_data_pipe, reading_service=reading_service)

        batch = next(iter(data_loader))
        self.assertEqual(batch, 0)

        state = data_loader.state_dict()
        self.assertIsNotNone(state)
        self.assertIsNotNone(state[SERIALIZED_DATAPIPE_KEY_NAME])
        self.assertIsNone(state[READING_SERVICE_STATE_KEY_NAME])
        data_loader.shutdown()

        test_data_pipe_2 = IterableWrapper(range(5))
        restored_data_loader = DataLoader2(datapipe=test_data_pipe_2, reading_service=reading_service)
        restored_data_loader.load_state_dict(state)

        restored_data_loader_datapipe = restored_data_loader.datapipe
        deserialized_datapipe = pickle.loads(state[SERIALIZED_DATAPIPE_KEY_NAME])
        for batch_1, batch_2 in zip(restored_data_loader_datapipe, deserialized_datapipe):
            self.assertEqual(batch_1, batch_2)

        self.assertNotEqual(
            len(restored_data_loader.datapipe),
            len(test_data_pipe_2),
        )

        self.assertEqual(
            restored_data_loader.reading_service_state,
            state[READING_SERVICE_STATE_KEY_NAME],
        )

        restored_data_loader.shutdown()


class DataLoader2ConsistencyTest(TestCase):
    r"""
    These tests ensure that the behaviors of `DataLoader2` are consistent across `ReadingServices` and potentially
    with `DataLoaderV1`.
    """

    def test_dataloader2_batch_collate(self) -> None:
        dp: IterDataPipe = IterableWrapper(range(10)).batch(2).collate()  # type: ignore[assignment]

        dl_no_rs: DataLoader2 = DataLoader2(dp)

        rs = MultiProcessingReadingService(num_workers=0)
        dl_multi_rs: DataLoader2 = DataLoader2(dp, reading_service=rs)

        self.assertTrue(all(x.eq(y).all() for x, y in zip(dl_no_rs, dl_multi_rs)))

    def test_dataloader2_shuffle(self) -> None:
        # TODO
        pass


if __name__ == "__main__":
    unittest.main()
