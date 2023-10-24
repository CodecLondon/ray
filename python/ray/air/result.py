import os
import io
import json
import pandas as pd
import pyarrow
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import ray
from ray.air.constants import (
    EXPR_RESULT_FILE,
    EXPR_PROGRESS_FILE,
    EXPR_ERROR_PICKLE_FILE,
)
from ray.util.annotations import PublicAPI

import logging

if TYPE_CHECKING:
    from ray.train import Checkpoint

logger = logging.getLogger(__name__)


@PublicAPI(stability="stable")
@dataclass
class Result:
    """The final result of a ML training run or a Tune trial.

    This is the class produced by Trainer.fit().
    It contains a checkpoint, which can be used for resuming training and for
    creating a Predictor object. It also contains a metrics object describing
    training metrics. ``error`` is included so that unsuccessful runs
    and trials can be represented as well.

    The constructor is a private API.

    Attributes:
        metrics: The final metrics as reported by a Trainable.
        checkpoint: The final checkpoint of the Trainable.
        error: The execution error of the Trainable run, if the trial finishes in error.
        metrics_dataframe: The full result dataframe of the Trainable.
            The dataframe is indexed by iterations and contains reported
            metrics.
        best_checkpoints: A list of tuples of the best checkpoints saved
            by the Trainable and their associated metrics. The number of
            saved checkpoints is determined by the ``checkpoint_config``
            argument of ``run_config`` (by default, all checkpoints will
            be saved).
    """

    metrics: Optional[Dict[str, Any]]
    checkpoint: Optional["Checkpoint"]
    error: Optional[Exception]
    metrics_dataframe: Optional["pd.DataFrame"] = None
    best_checkpoints: Optional[List[Tuple["Checkpoint", Dict[str, Any]]]] = None
    _local_path: Optional[str] = None
    _remote_path: Optional[str] = None
    _storage_filesystem: Optional[pyarrow.fs.FileSystem] = None
    _items_to_repr = ["error", "metrics", "path", "filesystem", "checkpoint"]

    @property
    def config(self) -> Optional[Dict[str, Any]]:
        """The config associated with the result."""
        if not self.metrics:
            return None
        return self.metrics.get("config", None)

    @property
    def path(self) -> str:
        """Path pointing to the result directory on persistent storage.

        This can point to a remote storage location (e.g. S3) or to a local
        location (path on the head node). The path is accessible via the result's
        associated `filesystem`.

        For instance, for a result stored in S3 at ``s3://bucket/location``,
        ``path`` will have the value ``bucket/location``.
        """
        return self._remote_path or self._local_path

    @property
    def filesystem(self) -> pyarrow.fs.FileSystem:
        """Return the filesystem that can be used to access the result path.

        Returns:
            pyarrow.fs.FileSystem implementation.
        """
        return self._storage_filesystem or pyarrow.fs.LocalFileSystem()

    def _repr(self, indent: int = 0) -> str:
        """Construct the representation with specified number of space indent."""
        from ray.tune.result import AUTO_RESULT_KEYS
        from ray.tune.experimental.output import BLACKLISTED_KEYS

        shown_attributes = {k: getattr(self, k) for k in self._items_to_repr}
        if self.error:
            shown_attributes["error"] = type(self.error).__name__
        else:
            shown_attributes.pop("error")

        shown_attributes["filesystem"] = shown_attributes["filesystem"].type_name

        if self.metrics:
            exclude = set(AUTO_RESULT_KEYS)
            exclude.update(BLACKLISTED_KEYS)
            shown_attributes["metrics"] = {
                k: v for k, v in self.metrics.items() if k not in exclude
            }

        cls_indent = " " * indent
        kws_indent = " " * (indent + 2)

        kws = [
            f"{kws_indent}{key}={value!r}" for key, value in shown_attributes.items()
        ]
        kws_repr = ",\n".join(kws)
        return "{0}{1}(\n{2}\n{0})".format(cls_indent, type(self).__name__, kws_repr)

    def __repr__(self) -> str:
        return self._repr(indent=0)

    @staticmethod
    def _validate_trial_dir(
        trial_dir: Union[str, os.PathLike], storage_filesystem: pyarrow.fs.FileSystem
    ):
        """Check the validity of the local or remote trial folder."""
        # TODO(ahmed-mahran): Fix circular dependency.
        from ray.train._internal.storage import _exists_at_fs_path

        if not _exists_at_fs_path(storage_filesystem, trial_dir):
            raise RuntimeError(f"Trial folder {trial_dir} doesn't exist!")

    @classmethod
    def from_path(
        cls,
        path: Union[str, os.PathLike],
        storage_filesystem: Optional[pyarrow.fs.FileSystem] = None,
    ) -> "Result":
        """Restore a Result object from local or remote trial directory.

        Args:
            path: A storage path or URI of the trial directory. (ex: s3://bucket/path
                or /tmp/ray_results)
            storage_filesystem: A custom filesystem to use. If not provided,
                this will be auto-resolved by pyarrow. If provided, the path
                is assumed to be prefix-stripped already, and must be a valid path
                on the filesystem.

        Returns:
            A :py:class:`Result` object of that trial.
        """
        # TODO(justinvyu): Fix circular dependency.
        from ray.train import Checkpoint
        from ray.train.constants import CHECKPOINT_DIR_NAME
        from ray.train._internal.storage import get_fs_and_path, _exists_at_fs_path

        fs, fs_path = get_fs_and_path(path, storage_filesystem)
        cls._validate_trial_dir(fs_path, fs)

        # Restore metrics from result.json
        result_json_file = os.path.join(fs_path, EXPR_RESULT_FILE)
        progress_csv_file = os.path.join(fs_path, EXPR_PROGRESS_FILE)
        if _exists_at_fs_path(fs, result_json_file):
            with fs.open_input_stream(result_json_file) as f:
                lines = f.readall().decode("utf-8").split("\n")
                json_list = [json.loads(line) for line in lines if line]
                metrics_df = pd.json_normalize(json_list, sep="/")
        # Fallback to restore from progress.csv
        elif _exists_at_fs_path(fs, progress_csv_file):
            with fs.open_input_stream(progress_csv_file) as f:
                metrics_df = pd.read_csv(io.StringIO(f.readall().decode("utf-8")))
        else:
            raise RuntimeError(
                f"Failed to restore the Result object: Neither {EXPR_RESULT_FILE}"
                f" nor {EXPR_PROGRESS_FILE} exists in the trial folder!"
            )

        latest_metrics = metrics_df.iloc[-1].to_dict() if not metrics_df.empty else {}

        # Restore all checkpoints from the checkpoint folders
        checkpoint_dirs = sorted(
            [
                item.base_name
                for item in fs.get_file_info(pyarrow.fs.FileSelector(fs_path))
                if item.type == pyarrow.fs.FileType.Directory
                and item.base_name.startswith("checkpoint_")
            ]
        )

        if checkpoint_dirs:
            checkpoints = [
                Checkpoint(os.path.join(fs_path, checkpoint_dir), fs)
                for checkpoint_dir in checkpoint_dirs
            ]

            metrics = []
            for checkpoint_dir in checkpoint_dirs:
                metrics_corresponding_to_checkpoint = metrics_df[
                    metrics_df[CHECKPOINT_DIR_NAME] == checkpoint_dir
                ]
                if metrics_corresponding_to_checkpoint.empty:
                    logger.warning(
                        "Could not find metrics corresponding to "
                        f"{checkpoint_dir}. These will default to an empty dict."
                    )
                metrics.append(
                    {}
                    if metrics_corresponding_to_checkpoint.empty
                    else metrics_corresponding_to_checkpoint.iloc[-1].to_dict()
                )

            latest_checkpoint = checkpoints[-1]
            # TODO(justinvyu): These are ordered by checkpoint index, since we don't
            # know the metric to order these with.
            best_checkpoints = list(zip(checkpoints, metrics))
        else:
            best_checkpoints = latest_checkpoint = None

        # Restore the trial error if it exists
        error = None
        error_file_path = os.path.join(fs_path, EXPR_ERROR_PICKLE_FILE)
        if _exists_at_fs_path(fs, error_file_path):
            with fs.open_input_stream(error_file_path) as f:
                error = ray.cloudpickle.load(f)

        return ray.air.result.Result(
            metrics=latest_metrics,
            checkpoint=latest_checkpoint,
            _local_path=path if isinstance(fs, pyarrow.fs.LocalFileSystem) else None,
            _remote_path=fs_path,
            _storage_filesystem=fs,
            metrics_dataframe=metrics_df,
            best_checkpoints=best_checkpoints,
            error=error,
        )

    @PublicAPI(stability="alpha")
    def get_best_checkpoint(self, metric: str, mode: str) -> Optional["Checkpoint"]:
        """Get the best checkpoint from this trial based on a specific metric.

        Any checkpoints without an associated metric value will be filtered out.

        Args:
            metric: The key for checkpoints to order on.
            mode: One of ["min", "max"].

        Returns:
            :class:`Checkpoint <ray.train.Checkpoint>` object, or None if there is
            no valid checkpoint associated with the metric.
        """
        if not self.best_checkpoints:
            raise RuntimeError("No checkpoint exists in the trial directory!")

        if mode not in ["max", "min"]:
            raise ValueError(
                f'Unsupported mode: {mode}. Please choose from ["min", "max"]!'
            )

        op = max if mode == "max" else min
        valid_checkpoints = [
            ckpt_info for ckpt_info in self.best_checkpoints if metric in ckpt_info[1]
        ]

        if not valid_checkpoints:
            raise RuntimeError(
                f"Invalid metric name {metric}! "
                f"You may choose from the following metrics: {self.metrics.keys()}."
            )

        return op(valid_checkpoints, key=lambda x: x[1][metric])[0]
