# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Tests to ensure that the training loop works with a dict (1.0)
"""
import os
from copy import deepcopy
from typing import Any, Callable
from unittest import mock

import pytest
import torch
from torch.utils.data import DataLoader
from torchmetrics import Accuracy, AveragePrecision

from pytorch_lightning import LightningModule, seed_everything
from pytorch_lightning.callbacks.base import Callback
from pytorch_lightning.trainer import Trainer
from pytorch_lightning.trainer.connectors.logger_connector.fx_validator import FxValidator
from pytorch_lightning.trainer.connectors.logger_connector.result import DefaultMetricsKeys, ResultCollection
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers.boring_model import BoringModel, RandomDataset
from tests.helpers.runif import RunIf


def test_fx_validator(tmpdir):
    funcs_name = sorted([f for f in dir(Callback) if not f.startswith('_')])

    callbacks_func = [
        'on_after_backward',
        'on_batch_end',
        'on_batch_start',
        'on_before_accelerator_backend_setup',
        'on_before_zero_grad',
        'on_epoch_end',
        'on_epoch_start',
        'on_fit_end',
        'on_configure_sharded_model',
        'on_fit_start',
        'on_init_end',
        'on_init_start',
        'on_keyboard_interrupt',
        'on_load_checkpoint',
        'on_pretrain_routine_end',
        'on_pretrain_routine_start',
        'on_sanity_check_end',
        'on_sanity_check_start',
        'on_save_checkpoint',
        'on_test_batch_end',
        'on_test_batch_start',
        'on_test_end',
        'on_test_epoch_end',
        'on_test_epoch_start',
        'on_test_start',
        'on_train_batch_end',
        'on_train_batch_start',
        'on_train_end',
        'on_train_epoch_end',
        'on_train_epoch_start',
        'on_train_start',
        'on_validation_batch_end',
        'on_validation_batch_start',
        'on_validation_end',
        'on_validation_epoch_end',
        'on_validation_epoch_start',
        'on_validation_start',
        "on_predict_batch_end",
        "on_predict_batch_start",
        "on_predict_end",
        "on_predict_epoch_end",
        "on_predict_epoch_start",
        "on_predict_start",
        'setup',
        'teardown',
    ]

    not_supported = [
        "on_before_accelerator_backend_setup",
        "on_fit_end",
        "on_fit_start",
        "on_configure_sharded_model",
        "on_init_end",
        "on_init_start",
        "on_keyboard_interrupt",
        "on_load_checkpoint",
        "on_pretrain_routine_end",
        "on_pretrain_routine_start",
        "on_sanity_check_end",
        "on_sanity_check_start",
        "on_predict_batch_end",
        "on_predict_batch_start",
        "on_predict_end",
        "on_predict_epoch_end",
        "on_predict_epoch_start",
        "on_predict_start",
        "on_save_checkpoint",
        "on_test_end",
        "on_train_end",
        "on_validation_end",
        "setup",
        "teardown",
    ]

    assert funcs_name == sorted(callbacks_func), (
        "Detected new callback function. Need to add its logging"
        " permission to FxValidator and update this test"
    )

    validator = FxValidator()

    for func_name in funcs_name:
        # This summarizes where and what is currently possible to log using `self.log`
        is_stage = "train" in func_name or "test" in func_name or "validation" in func_name
        is_start = "start" in func_name or "batch" in func_name
        on_step = is_stage and is_start
        on_epoch = True
        # creating allowed condition
        allowed = (
            is_stage or "batch" in func_name or "epoch" in func_name or "grad" in func_name or "backward" in func_name
        )
        allowed = (
            allowed and "pretrain" not in func_name and "predict" not in func_name
            and func_name not in ["on_train_end", "on_test_end", "on_validation_end"]
        )
        if allowed:
            validator.check_logging(fx_name=func_name, on_step=on_step, on_epoch=on_epoch)
            if not is_start and is_stage:
                with pytest.raises(MisconfigurationException, match="You can't"):
                    validator.check_logging(fx_name=func_name, on_step=True, on_epoch=on_epoch)
        else:
            assert func_name in not_supported
            with pytest.raises(MisconfigurationException, match="function doesn't support"):
                validator.check_logging(fx_name=func_name, on_step=on_step, on_epoch=on_epoch)

    with pytest.raises(RuntimeError, match="`foo` but it is not implemented"):
        validator.check_logging("foo", False, False)


@RunIf(min_gpus=2)
def test_epoch_results_cache_dp(tmpdir):

    root_device = torch.device("cuda", 0)

    class TestModel(BoringModel):

        def training_step(self, *args, **kwargs):
            result = super().training_step(*args, **kwargs)
            self.log("train_loss_epoch", result["loss"], on_step=False, on_epoch=True)
            return result

        def training_step_end(self, training_step_outputs):  # required for dp
            loss = training_step_outputs["loss"].mean()
            return loss

        def training_epoch_end(self, outputs):
            assert all(out["loss"].device == root_device for out in outputs)
            assert self.trainer.callback_metrics["train_loss_epoch"].device == root_device

        def validation_step(self, *args, **kwargs):
            val_loss = torch.rand(1, device=torch.device("cuda", 1))
            self.log("val_loss_epoch", val_loss, on_step=False, on_epoch=True)
            return val_loss

        def validation_epoch_end(self, outputs):
            assert all(loss.device == root_device for loss in outputs)
            assert self.trainer.callback_metrics["val_loss_epoch"].device == root_device

        def test_step(self, *args, **kwargs):
            test_loss = torch.rand(1, device=torch.device("cuda", 1))
            self.log("test_loss_epoch", test_loss, on_step=False, on_epoch=True)
            return test_loss

        def test_epoch_end(self, outputs):
            assert all(loss.device == root_device for loss in outputs)
            assert self.trainer.callback_metrics["test_loss_epoch"].device == root_device

        def train_dataloader(self):
            return DataLoader(RandomDataset(32, 64), batch_size=4)

        def val_dataloader(self):
            return DataLoader(RandomDataset(32, 64), batch_size=4)

        def test_dataloader(self):
            return DataLoader(RandomDataset(32, 64), batch_size=4)

    model = TestModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        accelerator="dp",
        gpus=2,
        limit_train_batches=2,
        limit_val_batches=2,
        max_epochs=1,
    )
    trainer.fit(model)
    trainer.test(model, ckpt_path=None)


def test_can_return_tensor_with_more_than_one_element(tmpdir):
    """Ensure {validation,test}_step return values are not included as callback metrics. #6623"""

    class TestModel(BoringModel):

        def validation_step(self, batch, *args, **kwargs):
            return {"val": torch.tensor([0, 1])}

        def validation_epoch_end(self, outputs):
            # ensure validation step returns still appear here
            assert len(outputs) == 2
            assert all(list(d) == ["val"] for d in outputs)  # check keys
            assert all(torch.equal(d["val"], torch.tensor([0, 1])) for d in outputs)  # check values

        def test_step(self, batch, *args, **kwargs):
            return {"test": torch.tensor([0, 1])}

        def test_epoch_end(self, outputs):
            assert len(outputs) == 2
            assert all(list(d) == ["test"] for d in outputs)  # check keys
            assert all(torch.equal(d["test"], torch.tensor([0, 1])) for d in outputs)  # check values

    model = TestModel()
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=2, progress_bar_refresh_rate=0)
    trainer.fit(model)
    trainer.validate(model)
    trainer.test(model)


def test_logging_to_progress_bar_with_reserved_key(tmpdir):
    """ Test that logging a metric with a reserved name to the progress bar raises a warning. """

    class TestModel(BoringModel):

        def training_step(self, *args, **kwargs):
            output = super().training_step(*args, **kwargs)
            self.log("loss", output["loss"], prog_bar=True)
            return output

    model = TestModel()
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True)
    with pytest.warns(UserWarning, match="The progress bar already tracks a metric with the .* 'loss'"):
        trainer.fit(model)


@pytest.mark.parametrize("add_dataloader_idx", [False, True])
def test_auto_add_dataloader_idx(tmpdir, add_dataloader_idx):
    """ test that auto_add_dataloader_idx argument works """

    class TestModel(BoringModel):

        def val_dataloader(self):
            dl = super().val_dataloader()
            return [dl, dl]

        def validation_step(self, *args, **kwargs):
            output = super().validation_step(*args[:-1], **kwargs)
            if add_dataloader_idx:
                name = "val_loss"
            else:
                name = f"val_loss_custom_naming_{args[-1]}"

            self.log(name, output["x"], add_dataloader_idx=add_dataloader_idx)
            return output

    model = TestModel()
    model.validation_epoch_end = None

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=2,
        limit_train_batches=2,
        limit_val_batches=2,
        num_sanity_val_steps=0,
    )
    trainer.fit(model)
    logged = trainer.logged_metrics

    # Check that the correct keys exist
    if add_dataloader_idx:
        assert 'val_loss/dataloader_idx_0' in logged
        assert 'val_loss/dataloader_idx_1' in logged
    else:
        assert 'val_loss_custom_naming_0' in logged
        assert 'val_loss_custom_naming_1' in logged


def test_metrics_reset(tmpdir):
    """Tests that metrics are reset correctly after the end of the train/val/test epoch."""

    class TestModel(LightningModule):

        def __init__(self):
            super().__init__()
            self.layer = torch.nn.Linear(32, 1)

            for stage in ['train', 'val', 'test']:
                acc = Accuracy()
                acc.reset = mock.Mock(side_effect=acc.reset)
                ap = AveragePrecision(num_classes=1, pos_label=1)
                ap.reset = mock.Mock(side_effect=ap.reset)
                self.add_module(f"acc_{stage}", acc)
                self.add_module(f"ap_{stage}", ap)

        def forward(self, x):
            return self.layer(x)

        def _step(self, stage, batch):
            labels = (batch.detach().sum(1) > 0).float()  # Fake some targets
            logits = self.forward(batch)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, labels.unsqueeze(1))
            probs = torch.sigmoid(logits.detach())
            self.log(f"loss/{stage}", loss)

            acc = self._modules[f"acc_{stage}"]
            ap = self._modules[f"ap_{stage}"]

            labels_int = labels.to(torch.long)
            acc(probs.flatten(), labels_int)
            ap(probs.flatten(), labels_int)

            # Metric.forward calls reset so reset the mocks here
            acc.reset.reset_mock()
            ap.reset.reset_mock()

            self.log(f"{stage}/accuracy", acc, lightning_attribute_name=f"acc_{stage}")
            self.log(f"{stage}/ap", ap, lightning_attribute_name=f"ap_{stage}")

            return loss

        def training_step(self, batch, batch_idx, *args, **kwargs):
            return self._step('train', batch)

        def validation_step(self, batch, batch_idx, *args, **kwargs):
            return self._step('val', batch)

        def test_step(self, batch, batch_idx, *args, **kwargs):
            return self._step('test', batch)

        def configure_optimizers(self):
            optimizer = torch.optim.SGD(self.layer.parameters(), lr=0.1)
            lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
            return [optimizer], [lr_scheduler]

        def train_dataloader(self):
            return DataLoader(RandomDataset(32, 64))

        def val_dataloader(self):
            return DataLoader(RandomDataset(32, 64))

        def test_dataloader(self):
            return DataLoader(RandomDataset(32, 64))

        def _assert_epoch_end(self, stage):
            acc = self._modules[f"acc_{stage}"]
            ap = self._modules[f"ap_{stage}"]

            acc.reset.assert_called_once()
            ap.reset.assert_called_once()

        def on_train_end(self):
            self._assert_epoch_end('train')

        def on_validation_end(self):
            if not self.trainer.sanity_checking:
                self._assert_epoch_end('val')

        def on_test_end(self):
            if not self.trainer.sanity_checking:
                self._assert_epoch_end('test')

    def _assert_called(model, stage):
        acc = model._modules[f"acc_{stage}"]
        ap = model._modules[f"ap_{stage}"]

        assert acc.reset.call_count == 1
        acc.reset.reset_mock()

        assert ap.reset.call_count == 1
        ap.reset.reset_mock()

    model = TestModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        max_epochs=1,
        progress_bar_refresh_rate=0,
        num_sanity_val_steps=2,
    )

    trainer.fit(model)
    _assert_called(model, 'train')
    _assert_called(model, 'val')

    trainer.validate(model)
    _assert_called(model, 'val')

    trainer.test(model)
    _assert_called(model, 'test')


def test_result_collection_on_tensor_with_mean_reduction():
    seed_everything(42)

    result_collection = ResultCollection(True, torch.device("cpu"))

    for i in range(1, 10):

        result_collection.batch_idx = i

        for prob_bar in [False, True]:

            for logger in [False, True]:

                i = float(i)

                result_collection.log(
                    "training_step",
                    f"loss_1_{int(prob_bar)}_{int(logger)}",
                    torch.tensor(i),
                    on_step=True,
                    on_epoch=True,
                    batch_size=i**2,
                    prog_bar=prob_bar,
                    logger=logger
                )
                result_collection.log(
                    "training_step",
                    f"loss_2_{int(prob_bar)}_{int(logger)}",
                    torch.tensor(i),
                    on_step=False,
                    on_epoch=True,
                    batch_size=i**2,
                    prog_bar=prob_bar,
                    logger=logger
                )
                result_collection.log(
                    "training_step",
                    f"loss_3_{int(prob_bar)}_{int(logger)}",
                    torch.tensor(i),
                    on_step=True,
                    on_epoch=False,
                    batch_size=i**2,
                    prog_bar=prob_bar,
                    logger=logger
                )
                result_collection.log(
                    "training_step",
                    f"loss_4_{int(prob_bar)}_{int(logger)}",
                    torch.tensor(i),
                    on_step=False,
                    on_epoch=False,
                    batch_size=i**2,
                    prog_bar=prob_bar,
                    logger=logger
                )

    excepted_values = [
        torch.tensor(1.),
        torch.tensor(2.),
        torch.tensor(3.),
        torch.tensor(4.),
        torch.tensor(5.),
        torch.tensor(6.),
        torch.tensor(7.),
        torch.tensor(8.),
        torch.tensor(9.)
    ]
    excepted_batches = [1, 4, 9, 16, 25, 36, 49, 64, 81]
    total_value = torch.tensor(excepted_values) * torch.tensor(excepted_batches)
    assert result_collection["training_step.loss_1_0_0"].value == sum(total_value)
    assert result_collection["training_step.loss_1_0_0"].cumulated_batch_size == sum(excepted_batches)

    batch_metrics = result_collection.get_batch_metrics()

    expected = {
        'loss_1_1_0_step': torch.tensor([9.]),
        'loss_3_1_0': torch.tensor([9.]),
        'loss_1_1_1_step': torch.tensor([9.]),
        'loss_3_1_1': torch.tensor([9.])
    }
    assert batch_metrics[DefaultMetricsKeys.PBAR] == expected

    excepted = {
        'loss_1_0_1_step': torch.tensor([9.]),
        'loss_3_0_1': torch.tensor([9.]),
        'loss_1_1_1_step': torch.tensor([9.]),
        'loss_3_1_1': torch.tensor([9.])
    }
    assert batch_metrics[DefaultMetricsKeys.LOG] == excepted

    excepted = {
        'loss_1_0_0': torch.tensor(9.),
        'loss_1_0_0_step': torch.tensor(9.),
        'loss_3_0_0': torch.tensor(9.),
        'loss_1_0_1': torch.tensor(9.),
        'loss_1_0_1_step': torch.tensor(9.),
        'loss_3_0_1': torch.tensor(9.),
        'loss_1_1_0': torch.tensor(9.),
        'loss_1_1_0_step': torch.tensor(9.),
        'loss_3_1_0': torch.tensor(9.),
        'loss_1_1_1': torch.tensor(9.),
        'loss_1_1_1_step': torch.tensor(9.),
        'loss_3_1_1': torch.tensor(9.)
    }
    assert batch_metrics[DefaultMetricsKeys.CALLBACK] == excepted

    result_collection.on_epoch_end_reached = True

    epoch_metrics = result_collection.get_epoch_metrics()

    mean = (torch.tensor(excepted_values) * torch.tensor(excepted_batches)).sum() / sum(excepted_batches)

    expected = {'loss_1_1_0_epoch': mean, 'loss_2_1_0': mean, 'loss_1_1_1_epoch': mean, 'loss_2_1_1': mean}
    assert epoch_metrics[DefaultMetricsKeys.PBAR] == expected

    excepted = {'loss_1_0_1_epoch': mean, 'loss_2_0_1': mean, 'loss_1_1_1_epoch': mean, 'loss_2_1_1': mean}
    assert epoch_metrics[DefaultMetricsKeys.LOG] == excepted

    excepted = {
        'loss_1_0_0': mean,
        'loss_1_0_0_epoch': mean,
        'loss_2_0_0': mean,
        'loss_1_0_1': mean,
        'loss_1_0_1_epoch': mean,
        'loss_2_0_1': mean,
        'loss_1_1_0': mean,
        'loss_1_1_0_epoch': mean,
        'loss_2_1_0': mean,
        'loss_1_1_1': mean,
        'loss_1_1_1_epoch': mean,
        'loss_2_1_1': mean,
    }
    assert epoch_metrics[DefaultMetricsKeys.CALLBACK] == excepted
