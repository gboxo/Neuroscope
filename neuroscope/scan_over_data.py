# %%

from utils import *
import json
import os
import torch
from huggingface_hub import hf_hub_download, list_files_info
from safetensors import safe_open
from tqdm import tqdm

from sae_lens.training.config import LanguageModelSAERunnerConfig
from sae_lens.training.sparse_autoencoder import SparseAutoencoder
import torch.nn.functional as F
torch.set_grad_enabled(False)

# Code to automatically update the HookedTransformer code as its edited without restarting the kernel
@dataclass
class Config:
    model_name: str = "gpt2-small"
    data_name: str = "pile-10k"
    max_tokens: int = -1
    debug: bool = True
    batch_size: int = 100
    version: int = 3
    n_ctx: int = 32
    overwrite: bool = True
    use_pred_log_probs: bool = False
    use_max_neuron_act: bool = False
    use_feature_max_act: bool = True
    use_neuron_logit_attr: bool = False
    use_head_logit_attr: bool = False
    use_activation_stats: bool = False
    neuron_top_k: int = 20
    head_top_k: int = 200
    device = "cuda" if torch.cuda.is_available() else "cpu"
    def __post_init__(self):
        if "attn-only" in self.model_name:
            self.use_max_neuron_act = False
            self.use_neuron_logit_attr = False

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]):
        """
        Instantiates a `Config` from a Python dictionary of
        parameters.
        """
        return cls(**config_dict)

    def __get_item__(self, string):
        return self.__dict__[string]

    def to_dict(self):
        return self.__dict__

    def __repr__(self):
        return "Config:\n" + pprint.pformat(self.to_dict())


default_cfg = Config()
IN_IPYTHON = True
if not IN_IPYTHON:
    print("Updating config")
    cfg = arg_parse_update_cfg(default_cfg.to_dict())
    cfg = Config.from_dict(cfg)
    print(cfg)
else:
    print("In IPython, skipping config")
        "debug": True,
        "use_activation_stats": True,
        "use_feature_max_act": True,
        "model_name":"gpt2-small",
        "data_name":"pile-10k"
    }
    cfg = dict(default_cfg.to_dict())
    cfg.update(new_config)
    cfg = Config.from_dict(cfg)
    cfg.debug = True

if cfg.debug:
    cfg.max_tokens = 320000#int(2e6)
    cfg.batch_size = 1000
print(cfg)




# %%
# Define classes

"""
Test:
tens = torch.load("/home/gerard/workspace/gpt2-small/debug/full_pred_log_probs/code/solu-3l/pred_log_probs.pth")

i = 870
j = 532
print(tens[i, j])
model = HookedTransformer.from_pretrained("solu-3l")
dataset, tokens_name = get_dataset("c4")
tokens = dataset[i:i+1]['tokens'].to(cfg.device)
with torch.autocast("cuda", torch.bfloat16):
    logits = model(tokens)
    plps = model.loss_fn(logits, tokens, per_token=True)
print(plps[0, j])
"""


class PredLogProbs:
    def __init__(self, cfg: Config, model: HookedTransformer):
        self.cfg = cfg
        self.debug = self.cfg.debug
        if self.debug:
            self.base_dir = Path("/home/gerard/workspace/gpt2-small/debug/full_pred_log_probs") / cfg.data_name / cfg.model_name  # type: ignore
        else:
            self.base_dir = Path("/home/gerard/workspace/gpt2-small/full_pred_log_probs") / cfg.data_name / cfg.model_name  # type: ignore
        self.base_dir.mkdir(exist_ok=True, parents=True)

        if self.debug:
            self.save_dir = self.base_dir
        else:
            self.save_dir = self.base_dir / f"v{self.cfg.version}"
            assert (not self.cfg.overwrite) or (
                not self.save_dir.exists()
            ), f"Trying to overwrite existing dir: {self.save_dir}"

        self.cpu_plps = []
        self.gpu_plps = []
        self.max_gpu_len = 100

        self.model = model

    def step(self, logits, tokens):
        pred_log_probs = self.model.loss_fn(logits, tokens, per_token=True)
        self.gpu_plps.append(pred_log_probs.detach())
        if len(self.gpu_plps) > self.max_gpu_len:
            self.cpu_plps.append(torch.cat(self.gpu_plps, dim=0).detach().cpu())
            del self.gpu_plps
            self.gpu_plps = []

    def save(self):
        if self.gpu_plps:
            self.cpu_plps.append(torch.cat(self.gpu_plps, dim=0).detach().cpu())
        self.save_dir.mkdir(exist_ok=True)
        final_out = torch.cat(self.cpu_plps, dim=0)
        out_path = self.save_dir / "pred_log_probs.pth"
        torch.save(final_out, out_path)
        print("Saved Pred Log Probs to:", out_path)

    def log(self) -> dict:
        return {}

    def finish(self):
        self.save()


class BaseMaxTracker:
    def __init__(self, cfg: Config, model: HookedTransformer, name: str):
        self.cfg = cfg
        self.debug = self.cfg.debug
        self.model = model
        self.name = name

        if self.debug:
            self.base_dir = Path(f"/home/gerard/workspace/gpt2-small/debug/{name}") / cfg.data_name / cfg.model_name  # type: ignore
        else:
            self.base_dir = Path(f"/home/gerard/workspace/gpt2-small/{name}") / cfg.data_name / cfg.model_name  # type: ignore
        self.base_dir.mkdir(exist_ok=True, parents=True)

        if self.debug:
            self.save_dir = self.base_dir
        else:
            self.save_dir = self.base_dir / f"v{self.cfg.version}"
            assert (not self.cfg.overwrite) or (
                not self.save_dir.exists()
            ), f"Trying to overwrite existing dir: {self.save_dir}"

    def step(self, logits, tokens):
        pass

    def save(self):
        raise NotImplementedError

    def log(self) -> dict:
        return {}

    def finish(self):
        self.save()


class NeuronMaxAct(BaseMaxTracker):
    def __init__(self, cfg: Config, model: HookedTransformer):
        super().__init__(cfg, model, name="neuron_max_act")

        self.stores = []
        for layer in range(self.model.cfg.n_layers):
            store = MaxStore(self.cfg.neuron_top_k, self.model.cfg.d_mlp,device=self.cfg.device)
            self.stores.append(store)

            def update_max_act_hook(neuron_acts, hook, store):
                store.batch_update(
                    torch.max(neuron_acts, dim=1, keepdim=False)
                )

            if self.model.cfg.act_fn == "solu_ln":
                hook_fn = partial(update_max_act_hook, store=store)
                self.model.blocks[layer].mlp.hook_mid.add_hook(hook_fn)
            elif self.model.cfg.act_fn in ["gelu", "relu", "gelu_new"]:
                hook_fn = partial(update_max_act_hook, store=store)
                self.model.blocks[layer].mlp.hook_post.add_hook(hook_fn)
            else:
                raise ValueError(f"Invalid Act Fn: {self.model.cfg.act_fn}")

    def save(self):
        self.save_dir.mkdir(exist_ok=True)
        for layer, store in enumerate(self.stores):
            store.save(folder_name=str(layer), dir=self.save_dir)
        print(f"Saved {self.name} stores to:", self.save_dir)


class FeatureResidMaxAct(BaseMaxTracker):
    def __init__(self, cfg: Config, model: HookedTransformer, encoders: list[SparseAutoencoder]):
        super().__init__(cfg, model, name="feature_max_act")

        self.stores = []
        for layer in range(self.model.cfg.n_layers):
            encoder = encoders[layer]
            store = MaxStore(self.cfg.neuron_top_k, encoder.W_enc.detach().size(1),device=self.cfg.device)
            self.stores.append(store)

            def update_max_act_hook(neuron_acts, hook, store,encoder):
                feature_bias = encoder.b_enc
                x_cent  = neuron_acts - encoder.b_dec
                feat_acts_pre = einops.einsum(x_cent,encoder.W_enc,"batch seq d_in, d_in feats -> batch seq feats")
                feat_acts = F.relu(feat_acts_pre + feature_bias)
                store.batch_update(
                    torch.max(feat_acts, dim=1, keepdim=False)
                )

            if self.model.cfg.act_fn in ["gelu", "relu", "gelu_new"]:
                hook_fn = partial(update_max_act_hook, store=store,encoder = encoder)
                self.model.blocks[layer].hook_resid_pre.add_hook(hook_fn)
            else:
                raise ValueError(f"Invalid Act Fn: {self.model.cfg.act_fn}")

    def save(self):
        self.save_dir.mkdir(exist_ok=True)
        for layer, store in enumerate(self.stores):
            store.save(folder_name=str(layer), dir=self.save_dir)
        print(f"Saved {self.name} stores to:", self.save_dir)





class HeadLogitAttr(BaseMaxTracker):
    """Stores the max positive and max negative contribution from each head to the correct logit"""

    def __init__(self, cfg: Config, model: HookedTransformer):
        super().__init__(cfg, model, name="head_logit_attr")

        self.W_OU = einsum(
            "layer head_index d_head d_model, d_model d_vocab -> layer head_index d_head d_vocab",
            self.model.W_O,
            self.model.W_U,
        )

        self.head_zs = [None] * self.model.cfg.n_layers

        self.pos_store = MaxStore(
            self.cfg.head_top_k, self.model.cfg.n_heads * self.model.cfg.n_layers
        )
        self.neg_store = MaxStore(
            self.cfg.head_top_k, self.model.cfg.n_heads * self.model.cfg.n_layers
        )

        def cache_z_hook(z, hook, layer, head_zs):
            head_zs[layer] = z.detach()

        for layer in range(self.model.cfg.n_layers):
            self.model.blocks[layer].attn.hook_z.add_hook(
                partial(cache_z_hook, layer=layer, head_zs=self.head_zs)
            )

        self.ln_scale_cache = {}

        def cache_ln_scale_hook(ln_scale, hook, cache):
            cache["ln_scale"] = ln_scale.detach()

        self.model.ln_final.hook_scale.add_hook(
            partial(cache_ln_scale_hook, cache=self.ln_scale_cache)
        )

    def step(self, logits, tokens):
        weights_to_true_logit = self.W_OU[..., tokens]
        weights_to_true_logit = einops.rearrange(
            weights_to_true_logit,
            "layer head_index d_head batch pos -> batch pos (layer head_index) d_head",
        )

        # Same shape as weights_to_true_logit
        cached_z = torch.cat(self.head_zs, dim=-2)
        cached_ln_scale = self.ln_scale_cache["ln_scale"]

        head_to_true_logit = einops.reduce(
            cached_z * weights_to_true_logit,
            "batch pos component d_head -> batch pos component",
            "sum",
        )
        head_to_true_logit = head_to_true_logit / cached_ln_scale

        max_head_to_true_logit = einops.reduce(
            head_to_true_logit, "batch pos component -> batch component", "max"
        )
        self.pos_store.batch_update(max_head_to_true_logit)

        min_head_to_true_logit = einops.reduce(
            head_to_true_logit, "batch pos component -> batch component", "min"
        )
        self.neg_store.batch_update(-min_head_to_true_logit)

    def save(self):
        self.save_dir.mkdir(exist_ok=True)
        self.pos_store.save(self.save_dir, "pos")
        self.neg_store.save(self.save_dir, "neg")


class NeuronLogitAttr(BaseMaxTracker):
    """Stores the max direct contribution from each neuron to the correct logit."""

    def __init__(self, cfg: Config, model: HookedTransformer):
        super().__init__(cfg, model, name="neuron_logit_attr")

        self.W_out_U = einsum(
            "layer d_mlp d_model, d_model d_vocab -> layer d_mlp d_vocab",
            self.model.W_out,
            self.model.W_U,
        )

        self.cache = {}

        def cache_neuron_post_hook(act_pos, hook, layer, cache):
            cache[f"post_{layer}"] = act_pos.detach()

        self.stores = []
        for layer in range(self.model.cfg.n_layers):
            self.stores.append(
                MaxStore(self.cfg.neuron_top_k, self.model.cfg.d_mlp)
            )
            # hook_post means the post MLP hook in both gelu & solu
            self.model.blocks[layer].mlp.hook_post.add_hook(
                partial(cache_neuron_post_hook, layer=layer, cache=self.cache)
            )

        def cache_ln_scale_hook(ln_scale, hook, cache):
            cache["ln_scale"] = ln_scale.detach()

        self.model.ln_final.hook_scale.add_hook(
            partial(cache_ln_scale_hook, cache=self.cache)
        )

    def step(self, logits, tokens):
        weights_to_true_logit = self.W_out_U[..., tokens]
        weights_to_true_logit = einops.rearrange(
            weights_to_true_logit, "layer d_mlp batch pos -> layer batch pos d_mlp"
        )

        cached_ln_scale = self.cache["ln_scale"]
        for layer in range(self.model.cfg.n_layers):
            neuron_post = self.cache[f"post_{layer}"]
            weights = weights_to_true_logit[layer]
            neuron_logit_attr = weights * neuron_post
            scaled_neuron_logit_attr = neuron_logit_attr / cached_ln_scale
            max_logit_attr = einops.reduce(
                scaled_neuron_logit_attr, "batch pos d_mlp -> batch d_mlp", "max"
            )
            self.stores[layer].batch_update(max_logit_attr)

    def save(self):
        self.save_dir.mkdir(exist_ok=True)
        for layer, store in enumerate(self.stores):
            store.save(folder_name=str(layer), dir=self.save_dir)
        print(f"Saved {self.name} stores to:", self.save_dir)


class ActivationStats:
    def __init__(self, cfg, model):
        self.cfg = cfg
        self.model = model
        self.name = "activation_stats"

        self.debug = self.cfg.debug
        if self.debug:
            self.base_dir = Path(f"/home/gerard/workspace/gpt2-small/debug/{self.name}") / cfg.data_name / cfg.model_name  # type: ignore
        else:
            self.base_dir = Path(f"/home/gerard/workspace/gpt2-small/{self.name}") / cfg.data_name / cfg.model_name  # type: ignore
        self.base_dir.mkdir(exist_ok=True, parents=True)

        if self.debug:
            self.save_dir = self.base_dir
        else:
            self.save_dir = self.base_dir / f"v{self.cfg.version}"
            assert (not self.cfg.overwrite) or (
                not self.save_dir.exists()
            ), f"Trying to overwrite existing dir: {self.save_dir}"

        self.mean_cache = {}
        self.sq_cache = {}

        def caching_hook(act, hook):
            self.mean_cache[hook.name] = act.mean(0)
            self.sq_cache[hook.name] = act.pow(2).mean(0)

        for hook_point in model.hook_dict.values():
            hook_point.add_hook(caching_hook)

    def step(self, logits, tokens):
        pass

    def save(self):
        self.save_dir.mkdir(exist_ok=True)
        torch.save(self.mean_cache, self.save_dir / "mean_act.pth")
        torch.save(self.sq_cache, self.save_dir / "sqaure_act.pth")
        print("Saved activation stats to:", self.save_dir)

    def finish(self):
        self.save()


def get_gpt2_res_jb_saes() -> (
    tuple[dict[str, SparseAutoencoder], dict[str, torch.Tensor]]
):

    GPT2_SMALL_RESIDUAL_SAES_REPO_ID = "jbloom/GPT2-Small-SAEs-Reformatted"
    GPT2_SMALL_RESIDUAL_SAES_HOOK_POINTS = [
        f"blocks.{layer}.hook_resid_pre" for layer in range(12)
    ] + ["blocks.11.hook_resid_post"]

    saes = {}
    for hook_point in tqdm(GPT2_SMALL_RESIDUAL_SAES_HOOK_POINTS):
        # download the files required:
        FILENAME = f"{hook_point}/cfg.json"
        hf_hub_download(repo_id=GPT2_SMALL_RESIDUAL_SAES_REPO_ID, filename=FILENAME)

        FILENAME = f"{hook_point}/sae_weights.safetensors"
        path = hf_hub_download(
            repo_id=GPT2_SMALL_RESIDUAL_SAES_REPO_ID, filename=FILENAME
        )


        # Then use our function to download the files
        folder_path = os.path.dirname(path)
        sae = SparseAutoencoder.load_from_pretrained(folder_path)
        saes[hook_point] = sae

    return saes






if __name__ == "__main__":
    
    
    saes = get_gpt2_res_jb_saes()
        
    saes = list(saes.values())
    if not cfg.debug:
        wandb.init(config=cfg.to_dict())
    model = HookedTransformer.from_pretrained(cfg.model_name)  # type: ignore
    dataset = get_dataset(cfg.data_name)
    if len(dataset) * cfg.n_ctx < cfg.max_tokens or cfg.max_tokens < 0:
        print("Resetting max tokens:", cfg.max_tokens, "to", len(dataset) * cfg.n_ctx)
        cfg.max_tokens = len(dataset) * cfg.n_ctx

    trackers = []
    if cfg.use_head_logit_attr:
        trackers.append(HeadLogitAttr(cfg, model))

    if cfg.use_max_neuron_act:
        trackers.append(NeuronMaxAct(cfg, model))

    if cfg.use_neuron_logit_attr:
        trackers.append(NeuronLogitAttr(cfg, model))

    if cfg.use_pred_log_probs:
        trackers.append(PredLogProbs(cfg, model))

    if cfg.use_activation_stats:
        trackers.append(ActivationStats(cfg, model))
    if cfg.use_feature_max_act:
        trackers.append(FeatureResidMaxAct(cfg, model,saes))

    try:
        print("1")
        with torch.autocast(str(cfg.device), torch.bfloat16):
            for index in tqdm(range(0, cfg.max_tokens // cfg.n_ctx, cfg.batch_size)):  # type: ignore
                tokens = dataset[index : index + cfg.batch_size].to(cfg.device)  # type: ignore

                logits = model(tokens).detach()
                for tracker in trackers:
                    tracker.step(logits, tokens)
                if not cfg.debug:
                    wandb.log({"tokens": index * cfg.n_ctx}, step=index)
    finally:
        for tracker in trackers:
            tracker.finish()
        if not cfg.debug:
            wandb.finish()


