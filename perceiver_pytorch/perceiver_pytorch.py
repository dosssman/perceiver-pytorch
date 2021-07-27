from math import pi, log
from functools import wraps
from typing import Optional

import torch
from torch import nn, einsum
import torch.nn.functional as F

from einops import rearrange, repeat

from perceiver_pytorch.rotary import SinusoidalEmbeddings, apply_rotary_emb

# helpers


def exists(val):
    return val is not None


def default(val, d):
    return val if exists(val) else d


def cache_fn(f):
    cache = None

    @wraps(f)
    def cached_fn(*args, _cache=True, **kwargs):
        if not _cache:
            return f(*args, **kwargs)
        nonlocal cache
        if cache is not None:
            return cache
        cache = f(*args, **kwargs)
        return cache

    return cached_fn


def fourier_encode(x, max_freq, num_bands=4, base=2):
    """Concatenate Fourier position features onto x.

    Args:
      x: Input data.
      max_freq: Maximum frequency.
      num_bands: Number of frequency bands to concatenate.
      base: Base of the logarithm function.
    """
    x = x.unsqueeze(-1)
    device, dtype, orig_x = x.device, x.dtype, x

    scales = torch.logspace(
        start=0.0,
        end=log(max_freq / 2) / log(base),
        steps=num_bands,  # Size of the 'scales' tensor.
        base=base,  # Base of the log function.
        device=device,
        dtype=dtype,
    )
    scales = scales[(*((None,) * (len(x.shape) - 1)), Ellipsis)]

    x = x * scales * pi
    x = torch.cat([x.sin(), x.cos()], dim=-1)
    x = torch.cat((x, orig_x), dim=-1)
    return x


# helper classes


class PreNorm(nn.Module):
    def __init__(self, dim, fn, context_dim=None):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)
        self.norm_context = (
            nn.LayerNorm(context_dim) if exists(context_dim) else None
        )

    def forward(self, x, **kwargs):
        x = self.norm(x)

        if exists(self.norm_context):
            context = kwargs["context"]
            normed_context = self.norm_context(context)
            kwargs.update(context=normed_context)

        return self.fn(x, **kwargs)


class GEGLU(nn.Module):
    """Gaussian Error Gated Linear Unit.

    See Shazer 2020: https://arxiv.org/abs/2002.05202
    """
    def forward(self, x):
        x, gates = x.chunk(2, dim=-1)
        return x * F.gelu(gates)


class FeedForward(nn.Module):
    """Feed forward neural net with GEGLU activation."""

    def __init__(self, dim: int, mult: int = 4, dropout: float = 0.0):
        """
        Args:
          dim: Input & Output size.
          mult: The inner dimension of the FF net will be dim * mult.
          dropout: Proportion to dropout after the GEGLU.
        """
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * mult * 2),
            GEGLU(),
            nn.Dropout(dropout),
            nn.Linear(dim * mult, dim),
        )

    def forward(self, x):
        return self.net(x)


class Attention(nn.Module):
    def __init__(
            self,
            query_dim: int,
            context_dim: Optional[int] = None,
            heads: int = 8,
            dim_head: int = 64,
            dropout: float = 0.0):
        """
        Args:
          query_dim: Size of the queries.
          context_dim: Size of the 'context' (the 'byte array' in the paper).
            If None, will default to the query_dim.
          heads: Number of attention heads.
          dim_head: Number of dimensions per head.
          dropout: Proportion to dropout (in the final linear layer).
        """
        super().__init__()
        inner_dim = dim_head * heads
        context_dim = default(context_dim, query_dim)

        self.scale = dim_head ** -0.5
        self.heads = heads

        # Network to generate queries ('q').
        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)

        # Network to generate keys and values ('k' and 'v').
        # Uses inner_dim * 2 out_features because the output is
        # split in two in forward() function.
        self.to_kv = nn.Linear(context_dim, inner_dim * 2, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, query_dim),
            nn.Dropout(dropout))

    def forward(self, x, context=None, mask=None, pos_emb=None):
        """
        Args:
          x: The 'latent array' in the Perceiver paper.
          context: The 'byte array' in the Perceiver paper (the input data).
        """
        h = self.heads

        q = self.to_q(x)  # Generate query.
        context = default(context, x)
        k, v = self.to_kv(context).chunk(2, dim=-1)

        # Rearrange the query, key and value tensors.
        # b = batch size; n =
        # h = number of heads; d = number of dims per head.
        q, k, v = map(
            lambda t: rearrange(t, "b n (h d) -> (b h) n d", h=h),
            (q, k, v)
        )

        if exists(pos_emb):
            q, k = apply_rotary_emb(q, k, pos_emb)

        sim = einsum("b i d, b j d -> b i j", q, k) * self.scale

        if exists(mask):
            mask = rearrange(mask, "b ... -> b (...)")
            max_neg_value = -torch.finfo(sim.dtype).max
            mask = repeat(mask, "b j -> (b h) () j", h=h)
            sim.masked_fill_(~mask, max_neg_value)

        # attention, what we cannot get enough of
        attn = sim.softmax(dim=-1)

        out = einsum("b i j, b j d -> b i d", attn, v)
        out = rearrange(out, "(b h) n d -> b n (h d)", h=h)
        return self.to_out(out)


# main class


class Perceiver(nn.Module):
    def __init__(
        self,
        *,
        num_freq_bands,
        depth,
        max_freq,
        freq_base=2,
        input_channels=3,
        input_axis=2,
        num_latents=512,
        latent_dim=512,
        cross_heads=1,
        latent_heads=8,
        cross_dim_head=64,
        latent_dim_head=64,
        num_classes=1000,
        attn_dropout=0.0,
        ff_dropout=0.0,
        weight_tie_layers=False,
        fourier_encode_data=True,
        self_per_cross_attn=1,
        self_attn_rel_pos=True
    ):
        """The shape of the final attention mechanism will be:
        depth * (cross attention -> self_per_cross_attn * self attention)

        Args:
          num_freq_bands: Number of freq bands, with original value (2 * K + 1)
          depth: Depth of net.
          max_freq: Maximum frequency, hyperparameter depending on how
              fine the data is.
          freq_base: Base of the logarithm function for Fourier position
              encoding.
          input_channels: Number of channels for each token of the input.
          input_axis: Number of axes for input data (2 for images, 3 for video)
          num_latents: Number of latents, or induced set points, or centroids.
              Different papers giving it different names.
          latent_dim: Latent dimension.
          cross_heads: Number of heads for cross attention. Paper said 1.
          latent_heads: Number of heads for latent self attention, 8.
          cross_dim_head: Number of dimensions per cross attention head.
          latent_dim_head: Number of dimensions per latent self attention head.
          num_classes: Output number of classes.
          attn_dropout:
          ff_dropout:
          weight_tie_layers: Whether to weight tie layers (optional).
          fourier_encode_data: Whether to auto-fourier encode the data, using
              the input_axis given. defaults to True, but can be turned off
              if you are fourier encoding the data yourself.
          self_per_cross_attn: Number of self attention blocks per cross attn.
          self_attn_rel_pos:
        """
        super().__init__()
        self.input_axis = input_axis
        self.max_freq = max_freq
        self.num_freq_bands = num_freq_bands
        self.freq_base = freq_base

        self.fourier_encode_data = fourier_encode_data
        fourier_channels = (
            (input_axis * ((num_freq_bands * 2) + 1))
            if fourier_encode_data
            else 0
        )
        input_dim = fourier_channels + input_channels

        # Randomly initialise the 'latent array'.
        self.latents = nn.Parameter(torch.randn(num_latents, latent_dim))

        def get_cross_attn():
            return PreNorm(
                latent_dim,
                Attention(
                    latent_dim,
                    input_dim,
                    heads=cross_heads,
                    dim_head=cross_dim_head,
                    dropout=attn_dropout,
                ),
                context_dim=input_dim)

        def get_cross_ff():
            return PreNorm(
                latent_dim,
                FeedForward(latent_dim, dropout=ff_dropout))

        def get_latent_attn():
            return PreNorm(
                latent_dim,
                Attention(
                    latent_dim,
                    heads=latent_heads,
                    dim_head=latent_dim_head,
                    dropout=attn_dropout,
                ))

        def get_latent_ff():
            return PreNorm(
                latent_dim,
                FeedForward(latent_dim, dropout=ff_dropout))

        # Cache all the above functions.
        get_cross_attn, get_cross_ff, get_latent_attn, get_latent_ff = map(
            cache_fn,
            (get_cross_attn, get_cross_ff, get_latent_attn, get_latent_ff))

        self.layers = nn.ModuleList([])
        for i in range(depth):
            should_cache = i > 0 and weight_tie_layers
            cache_args = {"_cache": should_cache}

            self_attns = nn.ModuleList([])

            for _ in range(self_per_cross_attn):
                self_attns.append(
                    nn.ModuleList(
                        [
                            get_latent_attn(**cache_args),
                            get_latent_ff(**cache_args),
                        ]
                    ))

            self.layers.append(
                nn.ModuleList(
                    [
                        get_cross_attn(**cache_args),
                        get_cross_ff(**cache_args),
                        self_attns,
                    ]
                ))

        self.to_logits = nn.Sequential(
            nn.LayerNorm(latent_dim),
            nn.Linear(latent_dim, num_classes))

        self.sinu_emb = None
        if self_attn_rel_pos:
            self.sinu_emb = SinusoidalEmbeddings(latent_dim_head)

    def forward(self, data, mask=None):
        b, *axis, _ = *data.shape
        device = data.device
        assert (
            len(axis) == self.input_axis
        ), f"Input data must have {self.input_axis} axes, not {len(axis)}!"

        if self.fourier_encode_data:
            # Calculate Fourier encoded positions in the range of [-1, 1],
            # for all axes.
            axis_pos = list(
                map(
                    lambda size:
                    torch.linspace(
                        -1.0, 1.0, steps=size, device=device
                    ),
                    axis))
            pos = torch.stack(torch.meshgrid(*axis_pos), dim=-1)
            enc_pos = fourier_encode(
                pos, self.max_freq, self.num_freq_bands, base=self.freq_base)
            enc_pos = rearrange(enc_pos, "... n d -> ... (n d)")
            enc_pos = repeat(enc_pos, "... -> b ...", b=b)

            data = torch.cat((data, enc_pos), dim=-1)

        # Concat to channels of data and flatten axis.
        # b = batch size; d = last dimension of data.
        data = rearrange(data, "b ... d -> b (...) d", b=b)

        # x is the 'latent array' in the paper.
        # b = batch size; n = number of latents; d = latent dimensions.
        x = repeat(self.latents, "n d -> b n d", b=b)

        # Rotary embeddings for latents, if specified.
        pos_emb = self.sinu_emb(x) if exists(self.sinu_emb) else None

        # Layers.
        for cross_attn, cross_ff, self_attns in self.layers:
            x = cross_attn(x, context=data, mask=mask) + x
            x = cross_ff(x) + x

            for self_attn, self_ff in self_attns:
                x = self_attn(x, pos_emb=pos_emb) + x
                x = self_ff(x) + x

        x = x.mean(dim=-2)
        return self.to_logits(x)
