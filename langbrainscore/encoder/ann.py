import typing
from enum import unique

import numpy as np
import torch
from tqdm import tqdm
import xarray as xr

from langbrainscore.dataset import Dataset
from langbrainscore.interface import EncoderRepresentations, _ModelEncoder
from langbrainscore.utils.encoder import (
    aggregate_layers,
    cos_sim_matrix,
    count_zero_threshold_values,
    flatten_activations_per_sample,
    get_context_groups,
    get_torch_device,
    pick_matching_token_ixs,
    postprocess_activations,
    repackage_flattened_activations,
    encode_stimuli_in_context,
)

from langbrainscore.utils.logging import log, get_verbosity
from langbrainscore.utils.xarray import copy_metadata, fix_xr_dtypes


class HuggingFaceEncoder(_ModelEncoder):
    def __init__(
        self,
        model_id,
        emb_aggregation: typing.Union[str, None, typing.Callable],
        device=get_torch_device(),
        context_dimension: str = None,
        bidirectional: bool = False,
        emb_preproc: typing.Tuple[str] = (),
        include_special_tokens: bool = True,
    ):
        """
        Args:
            model_id (str): the model id
            device (None, ?): the device to use
            context_dimension (str, optional): the dimension to use for extracting strings using context.
                if None, each sampleid (stimuli) will be treated as a single context group.
                if a string is specified, the string must refer to the name of a dimension in the xarray-like dataset
                object (langbrainscore.dataset.Dataset) that provides groupings of sampleids (stimuli) that should be
                used as context when generating encoder representations [default: None].
            bidirectional (bool): whether to use bidirectional encoder (i.e., access both forward and backward context)
                [default: False]
            emb_aggregation (typing.Union[str, None, typing.Callable], optional): how to aggregate the hidden states of
                the encoder representations for each sampleid (stimuli). [default: "last"]
            emb_preproc (tuple): a list of strings specifying preprocessing functions to apply to the aggregated embeddings.
                Processing is performed layer-wise.
            include_special_tokens (bool): whether to include special tokens in the encoder representations.
        """

        super().__init__(
            model_id,
            _context_dimension=context_dimension,
            _bidirectional=bidirectional,
            _emb_aggregation=emb_aggregation,
            _emb_preproc=emb_preproc,
            _include_special_tokens=include_special_tokens,
        )

        from transformers import AutoConfig, AutoModel, AutoTokenizer

        self.device = device or get_torch_device()
        self.config = AutoConfig.from_pretrained(self._model_id)
        self.tokenizer = AutoTokenizer.from_pretrained(self._model_id)
        self.model = AutoModel.from_pretrained(self._model_id, config=self.config)
        try:
            self.model = self.model.to(self.device)
        except RuntimeError:
            self.device = "cpu"
            self.model = self.model.to(self.device)

    def get_encoder_representations_template(
        self, dataset=None, representations=xr.DataArray()
    ) -> EncoderRepresentations:
        """
        returns an empty `EncoderRepresentations` object with all the appropriate
        attributes but the `dataset` and `representations` missing and to be filled in
        later.
        """
        return EncoderRepresentations(
            dataset=dataset,
            representations=representations,
            model_id=self._model_id,
            context_dimension=self._context_dimension,
            bidirectional=self._bidirectional,
            emb_aggregation=self._emb_aggregation,
            emb_preproc=self._emb_preproc,
            include_special_tokens=self._include_special_tokens,
        )

    def encode(
        self,
        dataset: Dataset,
        read_cache: bool = True,  # avoid recomputing if cached `EncoderRepresentations` exists, recompute if not
        write_cache: bool = True,  # dump the result of this computation to cache?
    ) -> EncoderRepresentations:
        """
        Input a langbrainscore Dataset, encode the stimuli according to the parameters specified in init, and return
            the an xarray DataArray of aggregated representations for each stimulus.

        Args:
            dataset (langbrainscore.dataset.DataSet): [description]
            read_cache (bool): Avoid recomputing if cached `EncoderRepresentations` exists, recompute if not
            write_cache (bool): Dump and write the result of the computed encoder representations to cache

        Raises:
            NotImplementedError: [description]
            ValueError: [description]

        Returns:
            [type]: [description]
        """

        # before computing the representations from scratch, we will first see if any
        # cached representations exist already.

        if read_cache:
            to_check_in_cache: EncoderRepresentations = (
                self.get_encoder_representations_template()
            )

            try:
                to_check_in_cache.load_cache()
                return to_check_in_cache
            except FileNotFoundError:
                log(
                    f"couldn't load cached reprs for [{to_check_in_cache.identifier_string}]",
                    cmap="WARN",
                    type="WARN",
                )

        self.model.eval()
        stimuli = dataset.stimuli.values

        # Initialize the context group coordinate (obtain embeddings with context)
        context_groups = get_context_groups(dataset, self._context_dimension)

        # list for storing activations for each stimulus with all layers flattened
        # list for storing layer ids ([0 0 0 0 ... 1 1 1 ...]) indicating which layer each
        # neuroid (representation dimension) came from
        flattened_activations, layer_ids = [], []

        ###############################################################################
        # ALL SAMPLES LOOP
        ###############################################################################
        _, unique_ixs = np.unique(context_groups, return_index=True)
        # Make sure context group order is preserved
        for group in tqdm(context_groups[np.sort(unique_ixs)]):
            # Mask based on the context group
            mask_context = context_groups == group
            stimuli_in_context = stimuli[mask_context]

            # store model states for each stimulus in this context group
            encoded_stimuli = []

            ###############################################################################
            # CONTEXT LOOP
            ###############################################################################
            for encoded_stim in encode_stimuli_in_context(
                stimuli_in_context=stimuli_in_context,
                tokenizer=self.tokenizer,
                model=self.model,
                bidirectional=self._bidirectional,
                include_special_tokens=self._include_special_tokens,
                emb_aggregation=self._emb_aggregation,
                device=self.device,
            ):
                encoded_stimuli += [encoded_stim]
            ###############################################################################
            # END CONTEXT LOOP
            ###############################################################################

            # Flatten activations across layers and package as xarray
            flattened_activations_and_layer_ids = [
                *map(flatten_activations_per_sample, encoded_stimuli)
            ]
            for f_as, l_ids in flattened_activations_and_layer_ids:
                flattened_activations += [f_as]
                layer_ids += [l_ids]
                assert len(f_as) == len(l_ids)  # Assert all layer lists are equal

        ###############################################################################
        # END ALL SAMPLES LOOP
        ###############################################################################

        # Stack flattened activations and layer ids to obtain [n_samples, emb_din * n_layers]
        activations_2d = np.vstack(flattened_activations)
        layer_ids_1d = np.squeeze(np.unique(np.vstack(layer_ids), axis=0))

        # Post-process activations
        if len(self._emb_preproc) > 0:
            for mode in self._emb_preproc:
                activations_2d, layer_ids_1d = postprocess_activations(
                    activations_2d=activations_2d,
                    layer_ids_1d=layer_ids_1d,
                    emb_preproc_mode=mode,
                )

        assert activations_2d.shape[1] == len(layer_ids_1d)
        assert activations_2d.shape[0] == len(stimuli)

        # Package activations as xarray and reapply metadata
        encoded_dataset: xr.DataArray = repackage_flattened_activations(
            activations_2d=activations_2d,
            layer_ids_1d=layer_ids_1d,
            dataset=dataset,
        )
        encoded_dataset: xr.DataArray = copy_metadata(
            encoded_dataset,
            dataset.contents,
            "sampleid",
        )

        to_return: EncoderRepresentations = self.get_encoder_representations_template()
        to_return.dataset = dataset
        to_return.representations = fix_xr_dtypes(encoded_dataset)

        if write_cache:
            to_return.to_cache(overwrite=True)

        return to_return

    def get_special_token_offset(self) -> int:
        """
        the offset (no. of tokens in tokenized text) from the start to exclude
        when extracting the representation of a particular stimulus. this is
        needed when the stimulus is evaluated in a context group to achieve
        correct boundaries (otherwise we get off-by-context errors)
        """
        with_special_tokens = self.tokenizer("brainscore")["input_ids"]
        first_token_id, *_ = self.tokenizer("brainscore", add_special_tokens=False)[
            "input_ids"
        ]
        special_token_offset = with_special_tokens.index(first_token_id)
        return special_token_offset

    def get_modelcard(self):
        """
        Returns the model card of the model
        NOT DONE!!!
        """

        # Obtain number of layers
        d_config = self.config.to_dict()

        config_specs_of_interest = [
            "n_layer",
            "n_ctx",
            "n_embd",
            "n_head",
            "vocab_size",
        ]

        config_specs = {k: d_config.get(k, None) for k in config_specs_of_interest}

        # Evaluate each layer

    def get_explainable_variance(
        self,
        ann_encoded_dataset,
        method: str = "pca",
        variance_threshold: float = 0.80,
        **kwargs,
    ) -> xr.Dataset:
        """
        Returns how many PCs are needed to explain the variance threshold (default 80%) per layer.

        TODO: move to `langbrainscore.analysis.?` or make @classmethod

        """
        # n_embd = self.config.n_embd

        # Get the PCA explained variance per layer
        layer_ids = ann_encoded_dataset.layer.values
        _, unique_ixs = np.unique(layer_ids, return_index=True)
        # Make sure context group order is preserved
        for layer_id in tqdm(layer_ids[np.sort(unique_ixs)]):
            layer_dataset = (
                ann_encoded_dataset.isel(
                    neuroid=(ann_encoded_dataset.layer == layer_id)
                )
                .drop("timeid")
                .squeeze()
            )
            # assert layer_dataset.shape[1] == n_embd

            # Figure out how many PCs we attempt to fit
            n_comp = np.min([layer_dataset.shape[1], layer_dataset.shape[0]])

            # Get explained variance
            if method == "pca":
                from sklearn.decomposition import PCA

                decomp = PCA(n_components=n_comp)
            elif method == "mds":
                from sklearn.manifold import MDS

                decomp = MDS(n_components=n_comp)
            elif method == "tsne":
                from sklearn.manifold import TSNE

                decomp = TSNE(n_components=n_comp)
            else:
                raise ValueError(f"Unknown method: {method}")

            decomp.fit(layer_dataset.values)
            explained_variance = decomp.explained_variance_ratio_

            # Get the number of PCs needed to explain the variance threshold
            explained_variance_cum = np.cumsum(explained_variance)
            n_pc_needed = np.argmax(explained_variance_cum >= variance_threshold) + 1

            # Store per layer
            layer_id = str(layer_id)
            print(
                f"Layer {layer_id}: {n_pc_needed} PCs needed to explain {variance_threshold} variance"
            )

    def get_layer_sparsity(
        self, ann_encoded_dataset, zero_threshold: float = 0.0001, **kwargs
    ) -> xr.Dataset:
        """
        Check how sparse activations within a given layer are.

        Sparsity is defined as 1 - values below the zero_threshold / total number of values.

        TODO: move to `langbrainscore.analysis.?` or make @classmethod
        """
        # n_embd = self.config.n_embd

        # Get the PCA explained variance per layer
        layer_ids = ann_encoded_dataset.layer.values
        _, unique_ixs = np.unique(layer_ids, return_index=True)
        # Make sure context group order is preserved
        for layer_id in tqdm(layer_ids[np.sort(unique_ixs)]):
            layer_dataset = (
                ann_encoded_dataset.isel(
                    neuroid=(ann_encoded_dataset.layer == layer_id)
                )
                .drop("timeid")
                .squeeze()
            )
            # assert layer_dataset.shape[1] == n_embd

            # Get sparsity
            zero_values = count_zero_threshold_values(
                layer_dataset.values, zero_threshold
            )
            sparsity = 1 - (zero_values / layer_dataset.size)

            # Store per layer
            layer_id = str(layer_id)
            print(f"Layer {layer_id}: {sparsity:.3f} sparsity")

    def get_anisotropy(self, ann_encoded_dataset: EncoderRepresentations):
        raise NotImplementedError


class PTEncoder(_ModelEncoder):
    def __init__(self, model_id: str) -> "PTEncoder":
        super().__init__(model_id)

    def encode(self, dataset: "langbrainscore.dataset.Dataset") -> xr.DataArray:
        # TODO
        ...


class EncoderCheck:
    """
    Class for checking whether obtained embeddings from the Encoder class are correct and similar to other encoder objects.
    """

    def __init__(
        self,
    ):
        pass

    def _load_cached_activations(self, encoded_ann_identifier: str):
        raise NotImplementedError

    def similiarity_metric_across_layers(
        self,
        sim_metric: str = "tol",
        enc1: xr.DataArray = None,
        enc2: xr.DataArray = None,
        tol: float = 1e-8,
        threshold: float = 1e-4,
    ) -> bool:
        """
        Given two activations, iterate across layers and check np.allclose using different tolerance levels.

                Parameters:
            sim_metric: str
                Similarity metric to use.
            enc1: xr.DataArray
                First encoder activations.
            enc2: xr.DataArray
                Second encoder activations.
            tol: float
                Tolerance level to start at (we will iterate upwards the tolerance level). Default is 1e-8.

                        Returns:
                                bool: whether the tolerance level was met (True) or not (False)
                                bad_stim: set of stimuli indices that did not meet tolerance level `threshold` (if any)

        """
        # First check is whether number of layers / shapes match
        assert enc1.shape == enc2.shape
        assert (
            enc1.sampleid.values == enc2.sampleid.values
        ).all()  # ensure that we are looking at the same stimuli
        layer_ids = enc1.layer.values
        _, unique_ixs = np.unique(layer_ids, return_index=True)
        print(f"\n\nChecking similarity across layers using sim_metric: {sim_metric}")

        all_good = True
        bad_stim = set()  # store indices of stimuli that are not similar

        # Iterate across layers
        for layer_id in tqdm(layer_ids[np.sort(unique_ixs)]):
            enc1_layer = enc1.isel(neuroid=(enc1.layer == layer_id))  # .squeeze()
            enc2_layer = enc2.isel(neuroid=(enc2.layer == layer_id))  # .squeeze()

            # Check whether values match. If not, iteratively increase tolerance until values match
            if sim_metric in ("tol", "diff"):
                abs_diff = np.abs(enc1_layer - enc2_layer)
                abs_diff_per_stim = np.max(
                    abs_diff, axis=1
                )  # Obtain the biggest difference aross neuroids (units)
                while (abs_diff_per_stim > tol).all():
                    tol *= 10

            elif "cos" in sim_metric:
                # Check cosine distance between each row, e.g., sentence vector
                cos_sim = cos_sim_matrix(enc1_layer, enc2_layer)
                cos_dist = (
                    1 - cos_sim
                )  # 0 means identical, 1 means orthogonal, 2 means opposite
                # We still want this as close to zero as possible for similar vectors.
                cos_dist_abs = np.abs(cos_dist)
                abs_diff_per_stim = cos_dist_abs

                # Check how close the cosine distance is to 0
                while (cos_dist_abs > tol).all():
                    tol *= 10
            else:
                raise NotImplementedError(f"Invalid `sim_metric`: {sim_metric}")

            print(f"Layer {layer_id}: Similarity at tolerance: {tol:.3e}")
            if tol > threshold:
                print(f"WARNING: Low tolerance level")
                all_good = False
                bad_stim.update(
                    enc1.sampleid[np.where(abs_diff_per_stim > tol)[0]]
                )  # get sampleids of stimuli that are not similar

        return all_good, bad_stim
