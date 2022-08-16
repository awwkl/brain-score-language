import logging
import numpy as np
from numpy.random import RandomState

from brainio.assemblies import DataAssembly
from brainscore_core.benchmarks import BenchmarkBase
from brainscore_core.metrics import Score, Metric
from brainscore_language import load_dataset, load_metric, benchmarks
from brainscore_language.artificial_subject import ArtificialSubject
from .data import BIBTEX

logger = logging.getLogger(__name__)


class Pereira2018ROI_LinregPearsonr(BenchmarkBase):
    """
    Evaluate model ability to predict fMRI data introduced in Pereira et al. 2018.
    This benchmark evaluates the fMRI data on the region-of-interest (ROI) level: each neural unit (neuroid) is the arithmetic
    mean of the voxels' responses within that ROI. 

    The mapping model is fitted on X folds of the data in k-fold cross-validation scheme, and the resulting predicted neuroid
    values are evaluated against the actual neuroid values via Pearson correlation.

    This benchmark builds off the neural benchmark introduced in Schrimpf et al. 2021, but:
    * contains re-modeled data (SPM12 as opposed to SPM5)
    * averages across voxels within ROIs
    """

    def __init__(self):
        self.data = load_dataset("Pereira2018ROI")
        self.metric = load_metric("pearsonr")
        ceiler = SplitHalvesConsistency( # todo! 
            num_splits=10, split_coordinate="subject_id", consistency_metric=self.metric
        )
        super(Pereira2018ROI_LinregPearsonr, self).__init__(
            identifier="Pereira2018ROI-linregpearsonr",
            version=1,
            parent="neural",
            ceiling_func=lambda: ceiler(self.data),
            bibtex=BIBTEX,
        )

    def __call__(self, candidate: ArtificialSubject) -> Score:
        candidate.perform_behavioral_task(ArtificialSubject.Task.reading_times)
        stimuli = self.data["word"].values
        predictions = candidate.digest_text(stimuli)["behavior"].values
        targets = self.data.mean("subject")  # compare to "average human"
        raw_score = self.metric(predictions, targets)
        score = ceiling_normalize(raw_score, self.ceiling)
        return score


def ceiling_normalize(raw_score, ceiling):
    # normalize by ceiling, but not above 1
    score = raw_score / ceiling
    score.attrs["raw"] = raw_score
    score.attrs["ceiling"] = ceiling
    if score > 1:
        overshoot_value = score.item()
        # ideally we would just update the value, but I could not figure out how to update a scalar DataArray
        attrs = score.attrs
        score = type(score)(1, coords=score.coords, dims=score.dims)
        score.attrs = attrs
        score.attrs["overshoot"] = overshoot_value
    return score


benchmarks["Pereira2018ROI-linregpearsonr"] = Pereira2018ROI_LinregPearsonr


class SplitHalvesConsistency:
    # following
    # https://github.com/brain-score/brain-score/blob/c51b8aa2c94212a9ac56c06c556afad0bb0a3521/brainscore/metrics/ceiling.py#L25-L96

    def __init__(
        self, num_splits: int, split_coordinate: str, consistency_metric: Metric
    ):
        """
        :param num_splits: how many times to create two halves
        :param split_coordinate: over which coordinate to split the assembly into halves
        :param consistency_metric: which metric to use to compute the consistency of two halves
        """
        self.num_splits = num_splits
        self.split_coordinate = split_coordinate
        self.consistency_metric = consistency_metric

    def __call__(self, assembly: DataAssembly) -> Score:
        split_dim = np.array(assembly[self.split_coordinate].dims).item()
        split_values = assembly[self.split_coordinate].values
        random_state = RandomState(0)
        consistencies = []
        splits = range(self.num_splits)
        for _ in splits:
            half1_values = random_state.choice(
                split_values, size=len(split_values) // 2, replace=False
            )
            half2_values = set(split_values) - set(
                half1_values
            )  # this only works because of `replace=False` above
            half1 = assembly[
                {split_dim: [value in half1_values for value in split_values]}
            ].mean(split_dim)
            half2 = assembly[
                {split_dim: [value in half2_values for value in split_values]}
            ].mean(split_dim)
            consistency = self.consistency_metric(half1, half2)
            # Spearman-Brown correction for sub-sampling
            corrected_consistency = 2 * consistency / (1 + (2 - 1) * consistency)
            consistencies.append(corrected_consistency)
        consistencies = Score(consistencies, coords={"split": splits}, dims=["split"])
        average_consistency = consistencies.median("split")
        average_consistency.attrs["raw"] = consistencies
        return average_consistency