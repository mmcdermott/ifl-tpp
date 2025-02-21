import torch
import torch.nn as nn

import torch.distributions as D
from dpp.distributions import Normal, MixtureSameFamily, TransformedDistribution
from dpp.utils import clamp_preserve_gradients
from typing import List, Optional, Sequence, Tuple, Union

from .recurrent_tpp import RecurrentTPP

SLICE_IDX_T = Union[int, slice, type(Ellipsis)]
class LogNormalMixtureDistribution(TransformedDistribution):
    """
    Mixture of log-normal distributions.

    We model it in the following way (see Appendix D.2 in the paper):

    x ~ GaussianMixtureModel(locs, log_scales, log_weights)
    y = std_log_inter_time * x + mean_log_inter_time
    z = exp(y)

    Args:
        locs: Location parameters of the component distributions,
            shape (batch_size, seq_len, num_mix_components)
        log_scales: Logarithms of scale parameters of the component distributions,
            shape (batch_size, seq_len, num_mix_components)
        log_weights: Logarithms of mixing probabilities for the component distributions,
            shape (batch_size, seq_len, num_mix_components)
        mean_log_inter_time: Average log-inter-event-time, see dpp.data.dataset.get_inter_time_statistics
        std_log_inter_time: Std of log-inter-event-times, see dpp.data.dataset.get_inter_time_statistics
    """
    def __init__(
        self,
        locs: Optional[torch.Tensor] = None,
        log_scales: Optional[torch.Tensor] = None,
        log_weights: Optional[torch.Tensor] = None,
        mean_log_inter_time: float = 0.0,
        std_log_inter_time: float = 1.0,
        direct_args: Optional[Tuple[D.Distribution, List[D.Transform]]] = None,
    ):
        self.mean_log_inter_time = mean_log_inter_time
        self.std_log_inter_time = std_log_inter_time
        if direct_args is None:
            mixture_dist = D.Categorical(logits=log_weights)
            component_dist = Normal(loc=locs, scale=log_scales.exp())
            GMM = MixtureSameFamily(mixture_dist, component_dist)
            if mean_log_inter_time == 0.0 and std_log_inter_time == 1.0:
                transforms = []
            else:
                transforms = [D.AffineTransform(loc=mean_log_inter_time, scale=std_log_inter_time)]
            transforms.append(D.ExpTransform())

            direct_args = (GMM, transforms)

        super().__init__(*direct_args)

    @property
    def mean(self) -> torch.Tensor:
        """
        Compute the expected value of the distribution.

        See https://github.com/shchur/ifl-tpp/issues/3#issuecomment-623720667

        Returns:
            mean: Expected value, shape (batch_size, seq_len)
        """
        a = self.std_log_inter_time
        b = self.mean_log_inter_time
        loc = self.base_dist._component_distribution.loc
        variance = self.base_dist._component_distribution.variance
        log_weights = self.base_dist._mixture_distribution.logits
        return (log_weights + a * loc + b + 0.5 * a**2 * variance).logsumexp(-1).exp()

    def __getitem__(self, index: Union[SLICE_IDX_T, Sequence[SLICE_IDX_T]]):
        if not isinstance(index, tuple): index = (index,)
        # We need to ensure the last axis is left unchanged, as that is the mixture aspect.
        index = index + (slice(None),)

        transforms = self.transforms
        base_dist = self.base_dist

        mixture_dist = base_dist.mixture_distribution
        mixture_logits = mixture_dist.logits

        curr_shape = mixture_logits.shape
        assert len(index) <= len(curr_shape), "Can't slice out the mixture axis!"

        sliced_logits = mixture_logits[index]
        sliced_mixture_dist = D.Categorical(logits=sliced_logits)

        component_dist = base_dist.component_distribution
        component_loc = component_dist.loc
        component_scale = component_dist.scale
        sliced_component_dist = Normal(
            loc = component_loc[index],
            scale = component_scale[index],
        )

        sliced_GMM = MixtureSameFamily(sliced_mixture_dist, sliced_component_dist)
        return type(self)(
            mean_log_inter_time=self.mean_log_inter_time, std_log_inter_time=self.std_log_inter_time,
            direct_args=(sliced_GMM, transforms)
        )

class LogNormMix(RecurrentTPP):
    """
    RNN-based TPP model for marked and unmarked event sequences.

    The marks are assumed to be conditionally independent of the inter-event times.

    The distribution of the inter-event times given the history is modeled with a LogNormal mixture distribution.

    Args:
        num_marks: Number of marks (i.e. classes / event types)
        mean_log_inter_time: Average log-inter-event-time, see dpp.data.dataset.get_inter_time_statistics
        std_log_inter_time: Std of log-inter-event-times, see dpp.data.dataset.get_inter_time_statistics
        context_size: Size of the context embedding (history embedding)
        mark_embedding_size: Size of the mark embedding (used as RNN input)
        num_mix_components: Number of mixture components in the inter-event time distribution.
        rnn_type: Which RNN to use, possible choices {"RNN", "GRU", "LSTM"}

    """

    def __init__(
        self,
        num_marks: int,
        mean_log_inter_time: float = 0.0,
        std_log_inter_time: float = 1.0,
        context_size: int = 32,
        mark_embedding_size: int = 32,
        num_mix_components: int = 16,
        rnn_type: str = "GRU",
    ):
        super().__init__(
            num_marks=num_marks,
            mean_log_inter_time=mean_log_inter_time,
            std_log_inter_time=std_log_inter_time,
            context_size=context_size,
            mark_embedding_size=mark_embedding_size,
            rnn_type=rnn_type,
        )
        self.num_mix_components = num_mix_components
        self.linear = nn.Linear(self.context_size, 3 * self.num_mix_components)

    def get_inter_time_dist(self, context: torch.Tensor) -> torch.distributions.Distribution:
        """
        Get the distribution over inter-event times given the context.

        Args:
            context: Context vector used to condition the distribution of each event,
                shape (batch_size, seq_len, context_size)

        Returns:
            dist: Distribution over inter-event times, has batch_shape (batch_size, seq_len)

        """
        raw_params = self.linear(context)  # (batch_size, seq_len, 3 * num_mix_components)
        # Slice the tensor to get the parameters of the mixture
        locs = raw_params[..., :self.num_mix_components]
        log_scales = raw_params[..., self.num_mix_components: (2 * self.num_mix_components)]
        log_weights = raw_params[..., (2 * self.num_mix_components):]

        log_scales = clamp_preserve_gradients(log_scales, -5.0, 3.0)
        log_weights = torch.log_softmax(log_weights, dim=-1)
        return LogNormalMixtureDistribution(
            locs=locs,
            log_scales=log_scales,
            log_weights=log_weights,
            mean_log_inter_time=self.mean_log_inter_time,
            std_log_inter_time=self.std_log_inter_time
        )
