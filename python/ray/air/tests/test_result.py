import os
import pyarrow
import pytest

import ray

from ray.air.constants import EXPR_RESULT_FILE
from ray import train
from ray.train import Result, CheckpointConfig, RunConfig, ScalingConfig
from ray.train.torch import TorchTrainer
from ray.train.base_trainer import TrainingFailedError
from ray.tune import TuneConfig, Tuner

from ray.train.tests.util import create_dict_checkpoint, load_dict_checkpoint
from ray.air.tests.test_remote_storage_hdfs import setup_hdfs


@pytest.fixture
def ray_start_4_cpus():
    address_info = ray.init(num_cpus=4)
    yield address_info
    # The code after the yield will run as teardown code.
    ray.shutdown()


def build_dummy_trainer(configs):
    def worker_loop():
        for i in range(configs["NUM_ITERATIONS"]):
            # Do some random reports in between checkpoints.
            train.report({"metric_a": -100, "metric_b": -100})

            if ray.train.get_context().get_world_rank() == 0:
                with create_dict_checkpoint({"iter": i}) as checkpoint:
                    train.report(
                        metrics={"metric_a": i, "metric_b": -i},
                        checkpoint=checkpoint,
                    )
            else:
                train.report(metrics={"metric_a": i, "metric_b": -i})
        raise RuntimeError()

    trainer = TorchTrainer(
        train_loop_per_worker=worker_loop,
        scaling_config=ScalingConfig(num_workers=2, use_gpu=False),
        run_config=RunConfig(
            name=configs["EXP_NAME"],
            storage_path=configs["STORAGE_PATH"],
            checkpoint_config=CheckpointConfig(
                num_to_keep=configs["NUM_CHECKPOINTS"],
                checkpoint_score_attribute="metric_a",
                checkpoint_score_order="max",
            ),
        ),
    )
    return trainer


def build_dummy_tuner(configs):
    return Tuner(
        trainable=build_dummy_trainer(configs), tune_config=TuneConfig(num_samples=1)
    )


@pytest.mark.parametrize("storage", ["local", "hdfs"])
@pytest.mark.parametrize("mode", ["trainer", "tuner"])
def test_result_restore(
    ray_start_4_cpus, setup_hdfs, monkeypatch, tmpdir, storage, mode
):
    monkeypatch.setenv("RAY_AIR_LOCAL_CACHE_DIR", str(tmpdir / "ray_results"))

    NUM_ITERATIONS = 5
    NUM_CHECKPOINTS = 3
    if storage == "local":
        storage_path = str(tmpdir)
    elif storage == "hdfs":
        hostname, port = setup_hdfs
        storage_path = f"hdfs://{hostname}:{port}{str(tmpdir)}"

    exp_name = "test_result_restore"

    configs = {
        "EXP_NAME": exp_name,
        "STORAGE_PATH": storage_path,
        "NUM_ITERATIONS": NUM_ITERATIONS,
        "NUM_CHECKPOINTS": NUM_CHECKPOINTS,
    }

    if mode == "trainer":
        trainer = build_dummy_trainer(configs)
        with pytest.raises(TrainingFailedError):
            trainer.fit()
    elif mode == "tuner":
        tuner = build_dummy_tuner(configs)
        tuner.fit()

    # Find the trial directory to restore
    exp_dir = os.path.join(storage_path, exp_name)
    fs, fs_exp_dir = pyarrow.fs.FileSystem.from_uri(exp_dir)
    for item in fs.get_file_info(pyarrow.fs.FileSelector(fs_exp_dir)):
        if item.type == pyarrow.fs.FileType.Directory and item.base_name.startswith(
            "TorchTrainer"
        ):
            trial_dir = os.path.join(exp_dir, item.base_name)
            break

    # [1] Restore from path
    result = Result.from_path(trial_dir)

    # Check if we restored all checkpoints
    assert result.checkpoint
    assert len(result.best_checkpoints) == NUM_CHECKPOINTS

    """
    Top-3 checkpoints with metrics:

                        | iter   | metric_a    metric_b
    checkpoint_000004        4            4          -4
    checkpoint_000003        3            3          -3
    checkpoint_000002        2            2          -2
    """
    # Check if the checkpoints bounded with correct metrics
    best_ckpt_a = result.get_best_checkpoint(metric="metric_a", mode="max")
    assert load_dict_checkpoint(best_ckpt_a)["iter"] == NUM_ITERATIONS - 1

    best_ckpt_b = result.get_best_checkpoint(metric="metric_b", mode="max")
    assert load_dict_checkpoint(best_ckpt_b)["iter"] == NUM_ITERATIONS - NUM_CHECKPOINTS

    with pytest.raises(RuntimeError, match="Invalid metric name.*"):
        result.get_best_checkpoint(metric="invalid_metric", mode="max")

    # Check if we properly restored errors
    assert isinstance(result.error, RuntimeError)

    # [2] Restore from path without result.json
    fs.delete_file(f"{trial_dir}/{EXPR_RESULT_FILE}")
    result = Result.from_path(trial_dir)

    # Do the same checks as above
    assert result.checkpoint
    assert len(result.best_checkpoints) == NUM_CHECKPOINTS

    best_ckpt_a = result.get_best_checkpoint(metric="metric_a", mode="max")
    assert load_dict_checkpoint(best_ckpt_a)["iter"] == NUM_ITERATIONS - 1

    best_ckpt_b = result.get_best_checkpoint(metric="metric_b", mode="max")
    assert load_dict_checkpoint(best_ckpt_b)["iter"] == NUM_ITERATIONS - NUM_CHECKPOINTS

    assert isinstance(result.error, RuntimeError)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-x", __file__]))
