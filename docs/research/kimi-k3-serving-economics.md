# Kimi K3 — Serving Economics and Hardware Analysis

**Status:** research input, not a design ruling
**Date:** 2026-07-28
**Author:** Claude (Opus 5) with founder
**rd:** `enterpriseaiframework-2d6`
**Public rendering:** `kimi-k3.html` in the `website` repo — single page, interactive calculators
inline (cost model, config comparator, MXFP4 datapath, HBM budget, roofline, topology picker),
scroll-spy nav, numbered references. Unlinked from site nav; publishes at `3dl.dev/kimi-k3.html`
once the website repo is pushed.

> **Continuation note.** Every number here is a *raw observation with a date and a source*, not a
> standing conclusion. GPU rental prices move weekly and the throughput figures are day-0. Before
> building on any figure below, re-check it against the cited source. The parts that will age
> best are the *architectural* facts (§2, §5) and the *shape* of the break-even arithmetic (§6);
> the parts that will age worst are every dollar figure.

---

## 0. Why this is in this repo

The Enterprise AI Framework brief (`docs/design/brief.md` §1) states the customer requirement as
*"I might rent GPU, I might buy GPU."* This document is the worked example of that decision for the
largest open-weight model available as of July 2026. It exists to answer, with sourced numbers,
the question every prospective operator asks first: **what does it cost to run the best open model
yourself, and when does that beat paying an API?**

It is deliberately *not* a cost estimate for our own deployment. `docs/design/deferred.md` closed
the local-model cost-accounting thread; this is customer-facing analysis, not a reopening of it.

Relevant standing constraint: **"Integrate, do not reimplement — we never write an inference engine
or a GPU price catalogue."** This document consumes vLLM/SGLang recipes and public provider prices.
It does not propose we maintain either.

---

## 1. The model

| Property | Value | Source |
|---|---|---|
| Total parameters | 2.8 T | HF model card |
| Activated parameters | 104 B (16 of 896 experts + 2 shared) | HF model card |
| Layers | 93 — 69 Kimi Delta Attention (linear) + 24 Gated MLA | HF model card |
| Native format | MXFP4 weights / MXFP8 activations, quantization-aware trained | HF model card |
| Context | 1,048,576 tokens | HF model card |
| HF repo size | **1.56 TB**, 96 safetensors shards (~1.4 TB of pure weight tensors) | HF file tree |
| License | Kimi K3 License — MIT-derived with revenue triggers (see §8) | Moonshot |
| Released | 2026-07-27 (weights); model available via API from 2026-07-16 | — |

Two architectural facts drive everything downstream:

1. **MXFP4 is native, not post-hoc.** The quantization-aware training is what preserves quality.
   You do not get to quantize further to fit cheaper hardware — 4 bits *is* the floor, and it is
   already priced into the 1.4 TB.
2. **69 of 93 layers are linear attention.** KV growth at long context is far gentler than a
   dense-attention model of this size. The 1.4 TB of weights is the binding constraint, not the
   cache. This is why 1 M context is tractable at all. See §1b for the exact memory arithmetic —
   the consequence is stranger than "cheaper KV".

## 1b. Cache memory, derived from `config.json`

Both figures below come from the published config, not estimation. Relevant values:
`kv_lora_rank: 512`, `qk_rope_head_dim: 64`, `full_attn_layers` (24 entries),
`kda_layers` (69 entries), `linear_attn_config.num_heads: 96`, `linear_attn_config.head_dim: 128`.

**MLA KV cache — per token.** Multi-head latent attention caches only the compressed latent plus
the decoupled RoPE key, shared across all heads:

```
(kv_lora_rank + qk_rope_head_dim) × MLA layers = (512 + 64) × 24 = 13,824 elements/token
  fp8  →  13,824 B  =  13.5 KiB/token     ← the recommended --kv-cache-dtype fp8
  bf16 →  27,648 B  =  27.0 KiB/token
```

*Method check:* the same formula on DeepSeek-V3 (61 layers × 576 × 2 B) yields 68.6 KiB/token,
matching its widely-cited ~70 KB figure.

**KDA recurrent state — per sequence.** Kimi Delta Attention is a delta-rule linear attention:
each head carries a fixed `d_k × d_v` matrix updated per chunk rather than appended to. It is
**constant in context length** and charged in full the moment a slot opens:

```
head_dim² × num_heads × KDA layers = 128² × 96 × 69 = 108,527,616 elements/sequence
  bf16 →  217 MB/sequence
  fp32 →  434 MB/sequence
```

**The consequence, which inverts the usual intuition.** At fp32 state and fp8 KV, one KDA slot
costs the same as **~31,400 tokens** of KV cache (at bf16/bf16, ~7,900). So below roughly 31k
context, **concurrency rather than context length is what fills HBM** — the reverse of nearly
every other model. Size a K3 deployment by slot count first, context second.

This is corroborated qualitatively by vLLM's production preview — *"A KDA state is much larger
than one ordinary token's KV entry"* — which is why they moved to large physical state blocks with
a separate prefix-match unit, and why SGLang exposes `--mamba-full-memory-ratio` as a first-class
tuning knob rather than an internal detail.

Worked example, 8× B300 (2,304 GB total; 1,400 weights + 64 runtime → 840 GB free), fp8 KV /
fp32 state:

| Slots × context | MLA KV | KDA state | Total | Verdict |
|---|---|---|---|---|
| 64 × 86k | 77 GB | 28 GB | 104 GB | fits easily |
| 256 × 128k | 464 GB | 111 GB | 575 GB | fits |
| 512 × 128k | 928 GB | 222 GB | 1,150 GB | over |
| 64 × 1M | 928 GB | 28 GB | 955 GB | over |

---

## 2. Hardware floor

vLLM's day-0 post is explicit: **"At least one 8× B300 (or GB300 NVL72) node is required; 16× B200
is also supported."** SGLang additionally lists H200/H100 and AMD MI350X/MI355X in its supported-GPU
matrix.

| Config | Aggregate HBM | Holds 1.4 TB? | Note |
|---|---|---|---|
| 8× B300 (288 GB) | 2.30 TB | yes, ~900 GB free | smallest sane box; single node, all NVLink |
| 8× B200 (180 GB) | 1.44 TB | technically, ~40 GB free | not viable — hence the 16× guidance |
| 16× B200 | 2.88 TB | yes | 2 nodes; needs real IB/RoCE |
| 16× H200 (141 GB) | 2.26 TB | yes | Hopper — see §5, big caveats |
| 8× H100 (80 GB) | 0.64 TB | **no** | not close |

**Distinction that matters:** appearing in a framework's supported-GPU list is *codebase support*.
Neither vLLM nor SGLang publishes a **validated 16× H200 recipe for K3**. Treat that configuration
as untested.

---

## 3. Throughput — what is actually published

All from the vLLM day-0 post, measured on **GB300 NVL72**:

| Mode | tok/s |
|---|---|
| Single user, TP8, batch 1 | 111 |
| Single user, TP16, batch 1 | 118 |
| Single user + DSpark speculative decode, TP8 | 331 |
| Single user + DSpark speculative decode, TP16 | **370** (3.14×) |
| High-throughput serving | **2,000+ TPGS** (tokens per GPU-second) |

DSpark acceptance rate: ~4.73 accepted tokens/step on coding and low-entropy text, ~2.61 on creative
writing. The 3.14× is workload-dependent and strongest exactly where an enterprise would use it.

### Caveats on the 2,000 TPGS figure — it drives all the $/token math

- It is measured on **GB300 NVL72**, a 72-GPU NVLink domain. That is close to an ideal
  expert-parallel fabric. An 8× B300 pod or a 2-node B200 cluster over InfiniBand will not match it.
- vLLM does not define TPGS in the post. Context implies tokens per GPU-second, and it almost
  certainly counts **prefill + decode combined**. Prefill tokens are cheap in bulk; decode is not.
  A decode-heavy workload will land far below 2,000.
- Consequence: every $/token figure below is banded across 500 / 1,000 / 2,000 TPGS rather than
  quoted at one number. **The 1,000 figure is the honest planning number for a discrete 8-GPU node.**

Hosted-API measured output speed, for comparison (Artificial Analysis): Fireworks 165 t/s
(13.2 s TTFT), Nebius 128 t/s, Together 55.6 t/s, Makora 48.2 t/s, Moonshot's own 33.3 t/s
(144 s TTFT).

---

## 4. Cost

### 4.1 API baseline (all providers, July 2026)

| Token type | Price |
|---|---|
| Cache-miss input | $3.00 / M |
| Cache-hit input | $0.30 / M |
| Output | $15.00 / M |
| Blended (7:2:1 cache-hit : input : output) | **$2.31 / M** |

Five providers: Kimi, Fireworks, Together, Makora, Nebius. Fireworks is fastest and lowest-latency.

### 4.2 Rental (RunPod on-demand, verified July 2026)

| Config | $/GPU-hr | Total $/hr | $/mo at 24/7 |
|---|---|---|---|
| 8× B300 (community) | $6.94 | $55.52 | $40,530 |
| 8× B300 (secure) | $7.39 | **$59.12** | $43,158 |
| 16× B200 cluster | $5.89 | $94.24 | $68,795 |
| 16× H200 (secure) | $4.39 | $70.24 | $51,275 |

Network volume: $0.05/GB/mo above 1 TB (standard), $0.14/GB/mo (high-performance).

Other providers: Nebius B200 $7.15 on-demand / $3.95 preemptible, H200 $4.50 / $2.45, storage
$0.065–0.08/GB/mo, up to 35% off for multi-month commitment. Lambda B200 ~$6.69–6.99/GPU-hr with
**22 TiB local SSD included** on multi-GPU nodes. Fireworks dedicated: B300 $12/GPU-hr,
B200 $10/GPU-hr, H100/H200 $7/GPU-hr — already hydrated, per-GPU-second billing, no start-up charge.

### 4.3 $/M tokens at 100% saturation

8× B300 @ $59.12/hr:

| Assumed TPGS | Aggregate tok/s | Tokens/hr | $/M tok |
|---|---|---|---|
| 2,000 (NVL72-class, optimistic) | 16,000 | 57.6 M | **$1.03** |
| 1,000 (realistic discrete node) | 8,000 | 28.8 M | **$2.05** |
| 500 (decode-heavy) | 4,000 | 14.4 M | **$4.11** |
| batch-1, no spec decode | 111 | 0.4 M | $148 |

16× B200 @ $94.24/hr: **$0.82 / $1.64 / $3.27** at the same three points.

That last row is the entire thesis in one line: **an unbatched K3 endpoint costs ~$148/M tokens.**
Saturation *is* the economics.

---

## 5. The Hopper question — is MXFP4 on H200 viable?

**Answer: it fits and it runs, but you pay ~17% more than an 8× B300 node for roughly one-seventh
the effective compute, and it fails specifically in the high-batch regime that is the only reason
to self-host.**

### 5.1 What the hardware does

Hopper (SM90) has **no FP4 tensor cores**. Blackwell has dedicated FP4 matrix instructions with
hardware microscaling; Hopper does not. MXFP4 on H100/H200 therefore runs as **W4A16** — reports
indicate vLLM pins the Marlin backend, the same path gpt-oss took in August 2025.

Mechanically:
- Weights **stay 4-bit in HBM**. They are not upconverted at load. This is why 1.4 TB still fits —
  you do not blow up to 5.6 TB BF16.
- Dequantization to BF16 happens in-kernel, per group of 32, immediately before the MMA.
- You get the **memory-bandwidth savings** of 4-bit weights. You get **none of the FP4 FLOPS**.

### 5.2 The two numbers

| | 8× B300 | 16× H200 |
|---|---|---|
| Aggregate HBM | 2.30 TB | 2.26 TB |
| 1.4 TB weights fit? | yes, ~900 GB free | yes, ~860 GB free |
| Math path | native MXFP4 | W4A16 → BF16 math |
| Per-GPU compute on that path | 15 PFLOPS dense FP4 | 989 TFLOPS dense BF16 |
| **Cluster compute** | **120 PFLOPS** | **15.8 PFLOPS** — 7.6× less |
| Per-GPU HBM bandwidth | 8 TB/s | 4.8 TB/s |
| **Aggregate bandwidth** | 64 TB/s | **76.8 TB/s** — 1.2× more |
| RunPod $/hr (secure) | $59.12 | $70.24 |
| Topology | 1 node, all NVLink | 2 nodes, IB between |

### 5.3 Where Hopper is fine and where it dies

- **Decode at low batch is bandwidth-bound.** The 16× H200 cluster has *more* aggregate HBM
  bandwidth than the 8× B300 node. Hopper is genuinely competitive here — this is the same reason
  W4A16 was worth shipping for gpt-oss on H100.
- **Prefill is compute-bound.** BF16 math against 7.6× less effective compute. Long prompts — the
  entire point of a 1 M-context model — will be brutal.
- **High batch is also compute-bound.** As concurrency rises, weight reads amortize across the
  batch and you cross from bandwidth-limited to compute-limited. That crossover is where the H200
  cluster falls off, and it is precisely where the self-hosting $/token case lives.

**Estimate, not measurement** (nobody has published K3-on-H200 throughput): roughly parity at
batch 1, **3–5× worse at serving batch sizes**, putting it around **$4–8/M tokens** versus $1–2/M
on the B300 node.

### 5.4 Two topology traps specific to 16× H200

1. **Do not use TP16.** Tensor parallel all-reduces every layer; across a node boundary that runs
   over IB (~400 GB/s) instead of NVLink (~900 GB/s), 93 layers deep. Use TP8 intra-node and
   expert parallel across (`--all2all-backend deepep_v2`).
2. **Do not use PP2 either, if you want DSpark.** SGLang requires `pp_size == 1` for speculative
   decoding. Pipeline-parallel across the two nodes is the obvious layout and it silently costs the
   3.14× decode speedup — the thing making Hopper decode tolerable. DSpark on Hopper is itself
   unvalidated; the draft model runs the same W4A16 path.

### 5.5 The only good reason to choose it

**Availability.** RunPod Instant Clusters offer H200 2-node with 3200 Gbps as a normal product;
B300 is not in the cluster lineup at all, and 8× B300 single pods are an availability lottery.
Nebius has H200 on-demand at $4.50 and preemptible at $2.45. If you need K3 running this week on
capacity that reliably exists, and the workload is short-prompt, low-concurrency, decode-heavy,
16× H200 is defensible. For $/token at saturation it is the wrong hardware at a higher price.

---

## 6. Buy versus rent

### 6.1 Capex

| Item | Cost |
|---|---|
| HGX B300 8-GPU server (Supermicro SYS-821GB-TNRX, Dell XE9712, Aivres, …) | $430k–$550k |
| DGX B300 (NVIDIA-branded) | $300k–$500k — quotes vary widely |
| Implied per-GPU | ~$37.5k–$44k |
| NVMe (~20 TB usable), 100 GbE uplink, rack/PDU | $15k–$30k |
| Liquid-cooling-capable colo install | $5k–$13k one-time |

**~$470k all-in to get one node racked and serving**, band $380k–$600k. Lead times: Supermicro
2–4 weeks, Gigabyte 3–5, Dell/Lenovo 6–8, HPE 6–10 — assuming a single-unit order from a small
buyer is quoted at all. Blackwell Ultra allocation prioritizes hyperscalers and large NCPs.

### 6.2 Opex

An HGX B300 8-GPU node caps at **1,100 W/GPU** (the 1,400 W figure is the GB300 NVL72 rack config).
That is 8.8 kW of GPU; NVIDIA measures the full DGX B300 system at **14.5 kW**.

| Line | $/mo |
|---|---|
| Colo power+space, 14.5 kW @ ~$200/kW (AI/high-density tier is $175–225) | $2,900 |
| Liquid / RDHx premium | $500–2,000 |
| Hidden (cross-connect, remote hands, power overage — 8–15% of headline) | ~$400 |
| **Colo subtotal** | **~$4,000** |
| Enterprise support/warranty (~10%/yr of hardware) | $3,750 |
| Cost of capital on $450k @ 8% | $3,000 |

### 6.3 Three-year TCO

| | Lean (cash, self-spare, cheap colo) | Full (support + financing, less 25% residual) |
|---|---|---|
| Capex | $450k | $470k |
| Colo × 36 mo | $115k | $144k |
| Support × 36 mo | — | $135k |
| Cost of capital | — | $108k |
| Residual at yr 3 | — | −$113k |
| **Total** | **$565k** | **$744k** |
| **Effective $/hr at 24/7** | **$21.50** | **$28.31** |
| **$/M tok @ 1,000 TPGS** | **$0.75** | **$0.98** |
| **$/M tok @ 2,000 TPGS** | **$0.37** | **$0.49** |

Owning is the only configuration where self-hosting K3 wins decisively: 2.4–3× on blended tokens,
15–40× on output tokens.

### 6.4 Break-even — the number that actually decides it

| Versus | Duty cycle to justify buying |
|---|---|
| RunPod 8× B300 @ $59.12/hr | **36–48%** |
| Fireworks dedicated @ $96/hr (8× B300) | **22–29%** |
| Fireworks shared API @ $2.31/M blended | **~2,600 tok/s sustained 24/7** (≈224 M tokens/day) |

Derivation of the last row: $565k ÷ $2.31/M = 244.6 B tokens over three years = 223 M/day
= 2,586 tok/s continuous.

### 6.5 Three things that kill it before the math matters

1. **It cannot go in a house.** 14.5 kW continuous is a dedicated 80 A/240 V circuit doing nothing
   else, plus 49,500 BTU/hr — over 4 tons of cooling, always on — plus liquid or hybrid cooling.
   For scale: the `mainframe` box peaks around 1.2 kW. Off by more than 10×. This is a colo line
   item from day one.
2. **It is a depreciating asset in a falling market.** B200 rental has already compressed from
   ~$10/hr to $5.89/hr. The 25% three-year residual assumed above is the optimistic case.
3. **You inherit the pager.** HBM failures, firmware, driver regressions, the liquid loop, and a
   1.4 TB model that takes minutes to reload after any of them.

### 6.6 The middle answer

At 20–50% duty cycle — where almost everyone actually lands — the answer is neither buy nor
on-demand but **commit**: Nebius multi-month reservations (up to 35% off, ~$4.65/GPU-hr B200) or
Lambda 1-Click Clusters (2 weeks to 1 year). Most of the ownership discount, none of the capital,
no three-year bet on Blackwell Ultra holding value.

Buy the node when a workload is *already* running above 2,600 tok/s sustained on rented hardware
and has been for six months.

---

## 7. Operational reality — the cold-start tax

The naive path is the expensive one and it is what every quickstart hands you. `vllm serve
moonshotai/Kimi-K3` and the SkyPilot YAML both pull from HF **at runtime, on the GPU node**:

| | |
|---|---|
| 1.56 TB @ 1 Gbps sustained | 3.5 hr × $59.12 = **$205** |
| 1.56 TB @ 2 Gbps | 1.7 hr = **$102** |
| One failed init that still bills | +$20–60 |

So ~$250 before first token. Hydrating on a CPU pod instead (~$0.20/hr) drops the one-time cost to
about $1. **The one-time cost is solvable. The recurring tax is not:**

- **$100/mo standing** for a 2 TB network volume, serving or not. You cannot power down and keep
  the weights for free.
- **$12–46 per cold start**, every start, reloading 1.4 TB from network volume into HBM. At
  $0.98/min for an 8× B300 pod, a 45-minute load is $44.
- The usual RunPod answer — bake the model into the container image so it lands on local NVMe —
  **inverts at this size.** A 1.5 TB image will choke the registry and the image cache. That trick
  works at 20 GB, not 1,500 GB.

### 7.1 Where the cold-start tax actually bites

| Monthly usage | RunPod 8× B300 (+volume +starts) | Fireworks dedicated 8× B300 | Winner |
|---|---|---|---|
| 730 hr (24/7) | $43,258 | $70,080 | RunPod by 62% |
| 100 hr, 20 starts | $6,512 | $9,600 | RunPod by 32% |
| 20 hr, 20 starts | $1,782 (28% waste) | $1,920 | wash |

The tax does not kill RunPod. It kills RunPod **for bursty use** — which is exactly the case you
would pick a cheap on-demand provider for. Saturated 24/7 it is a rounding error.

### 7.2 Mitigations, in order of preference

1. **Hydrate on a CPU pod**, never the GPU pod. `HF_HUB_ENABLE_HF_TRANSFER=1 hf download
   moonshotai/Kimi-K3 --local-dir …` against an attached network volume. Kill the CPU pod after.
2. **High-performance storage tier** ($0.14 vs $0.05/GB/mo — $280 vs $100/mo for 2 TB) buys up to
   3× throughput. Roughly 45 min → 15 min per load, saving ~$29/start. Breaks even at ~6 starts/mo.
3. **`--load-format fastsafetensors`** on the serve command. Cold-loading 1.4 TB is minutes, and
   you pay for every one.
4. **Change providers if bursty.** Lambda includes 22 TiB local SSD on multi-GPU nodes — weights on
   node-local NVMe, reload at NVMe speed, no storage line item at all. ~15% more per hour to delete
   the problem entirely; a good trade below ~200 hr/mo.
5. **Never use preemptible for this.** Nebius B200 preemptible at $3.95/GPU-hr is the cheapest
   Blackwell available, and it is wrong here: a preemption on a model that takes 15–45 min to
   reload costs more than the discount saves.

---

## 8. License

The **Kimi K3 License** — MIT-derived for most of its length (use, copy, modify, distribute,
sublicense, sell; run, deploy, fine-tune, build derivatives), with two commercial triggers:

- **Model-as-a-Service** — giving third parties inference or fine-tuning access with meaningful
  control over inputs, parameters, or training data — requires a separate agreement with Moonshot
  once revenue across licensee and affiliates passes **$20 M over any consecutive 12 months**.
- Any commercial product with **>100 M MAU or >$20 M monthly revenue** must display "Kimi K3"
  prominently in its interface.

This departs from the Modified MIT of earlier Kimi releases. **Relevance to this repo:** serving K3
internally behind the framework is unencumbered. Offering it to third parties as a hosted service
is the trigger — which is exactly the MaaS shape, and worth flagging to any customer who plans to
resell inference.

---

## 9. Deployment recipes

### 9.1 vLLM, single 8× B300 node

```bash
VLLM_USE_RUST_FRONTEND=1 vllm serve /workspace/kimi-k3 \
  --tensor-parallel-size 8 \
  --trust-remote-code \
  --load-format fastsafetensors \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser kimi_k3 \
  --reasoning-parser kimi_k3 \
  --moe-backend flashinfer_trtllm \
  --all2all-backend flashinfer_nvlink_one_sided \
  --kv-cache-dtype fp8 \
  --max-num-seqs 512 \
  --gpu-memory-utilization 0.9
```

For 16× B200 across two nodes: `--moe-backend deep_gemm_mega_moe`, `--all2all-backend deepep_v2`
(RDMA), and expert/data parallel across nodes rather than pipeline parallel (see §5.4).

### 9.2 SGLang

More mature recipe surface than vLLM as of release: prefill/decode disaggregation
(`--disaggregation-mode prefill|decode` with a routing layer on :8000), `--mamba-full-memory-ratio`
autotuning for the KDA layers, `--enable-dp-attention` / `--ep-size` for expert parallel, and named
Low-Latency / Balanced / High-Throughput / Long-Context configs. Speculative decoding:

```
--speculative-algorithm DSPARK \
--speculative-draft-model-path RadixArk/Kimi-K3-DSpark \
--speculative-dspark-block-size 7
```

Multi-node: `--nnodes`, `--node-rank`, `--dist-init-addr <head>:<port>`.

### 9.3 SkyPilot, without the managed-endpoints product

No K3 example exists yet; the Kimi-K2 multi-node example is a direct template. Requires
`skypilot-nightly >= 1.0.0.dev20251114` for `~/sky_templates/ray/start_cluster`.

```yaml
resources:
  accelerators: B200:8
  cpus: 100+
  memory: 1000+
  disk_size: 2000
  ports: 8000
  image_id: docker:vllm/vllm-openai:latest
num_nodes: 2

setup: |
  pip install blobfile
  HF_HUB_ENABLE_HF_TRANSFER=1 hf download moonshotai/Kimi-K3

run: |
  bash ~/sky_templates/ray/start_cluster
  if [ "$SKYPILOT_NODE_RANK" == "0" ]; then
    VLLM_USE_RUST_FRONTEND=1 vllm serve moonshotai/Kimi-K3 \
      --tensor-parallel-size $SKYPILOT_NUM_GPUS_PER_NODE \
      --pipeline-parallel-size $SKYPILOT_NUM_NODES \
      --trust-remote-code --load-format fastsafetensors \
      --tool-call-parser kimi_k3 --reasoning-parser kimi_k3 \
      --port 8000
  fi
```

`sky launch` for one instance; `sky serve up` for a load-balanced replica pool with preemption
replacement — neither requires the managed-endpoints product.

**Two warnings on that YAML.** The K2-style `TP8 + PP2` layout silently disables DSpark
(`pp_size == 1` required). And **multi-node on RunPod via SkyPilot is unverified** — RunPod's IB
fabric is provisioned through their own Instant Clusters API and no confirmation was found that
SkyPilot's RunPod provisioner wires it up. For genuine SkyPilot multi-node use Nebius, Lambda, GCP,
AWS, or a K8s cluster with RDMA — or sidestep it with a single 8× B300 node.

---

## 10. Bottom line

| If you are… | Do this |
|---|---|
| Output-heavy workload, any volume | Self-host — beats $15/M output API by 4–15× |
| Prompt-heavy with cache hits (agentic coding) | **Use the API.** $0.30/M cached input is unbeatable; Moonshot reports ~90% cache-hit rates on coding traffic via Mooncake |
| Bursty, <200 hr/mo | Rent from a provider with included local NVMe (Lambda), not RunPod |
| 20–50% duty cycle | Commit/reserve — Nebius or Lambda 1CC |
| >2,600 tok/s sustained for 6+ months | Buy — and budget the colo, not the garage |
| Need it running this week | 16× H200 on RunPod Instant Clusters, eyes open about §5 |

**Self-hosting K3 is a sovereignty, fine-tuning, or rate-limit decision, not a cost decision** —
unless you own the hardware and saturate it.

---

## 11. Sources

All accessed 2026-07-28.

| Claim area | Source |
|---|---|
| Model card, params, quantization, license | https://huggingface.co/moonshotai/Kimi-K3 |
| **`config.json`** — MLA/KDA dimensions behind the §1b derivation | https://huggingface.co/moonshotai/Kimi-K3/raw/main/config.json |
| vLLM production preview — KDA state blocks, prefix-match units | https://vllm.ai/blog/2026-07-22-kimi-k3-preview |
| Repo size, shard count | https://huggingface.co/moonshotai/Kimi-K3/tree/main |
| Hardware floor, throughput, vLLM flags | https://vllm.ai/blog/2026-07-27-k3 |
| SGLang recipes, DSpark, disaggregation | https://docs.sglang.io/cookbook/autoregressive/Moonshotai/Kimi-K3 |
| MXFP4-on-Hopper (W4A16 / Marlin precedent) | https://vllm.ai/blog/2025-08-05-gpt-oss |
| FP4 tensor cores by architecture | https://www.emergentmind.com/topics/fp4-tensor-cores |
| FP4 on Hopper (software emulation) | https://arxiv.org/pdf/2603.02731 |
| B300 specs — 15 PFLOPS dense FP4, 288 GB | https://www.tomshardware.com/pc-components/gpus/nvidia-announces-blackwell-ultra-b300-1-5x-faster-than-b200-with-288gb-hbm3e-and-15-pflops-dense-fp4 |
| H200 specs — 989 TFLOPS BF16, 4.8 TB/s | https://www.spheron.network/blog/nvidia-h200-specs/ |
| DGX B300 system power (14.5 kW) | https://docs.nvidia.com/dgx/dgxb300-user-guide/introduction-to-dgxb300.html |
| B300 1,400 W / data-center readiness | https://blog.barrack.ai/nvidia-b300-1400w-data-center-requirements/ |
| RunPod GPU + storage pricing | https://www.runpod.io/pricing |
| RunPod Instant Clusters | https://docs.runpod.io/instant-clusters |
| RunPod high-performance storage | https://docs.runpod.io/storage/high-performance-storage |
| RunPod K3 FAQ | https://www.runpod.io/articles/guides/kimi-k3-technical-faq |
| Nebius pricing + storage | https://nebius.com/prices |
| Lambda pricing + included SSD | https://lambda.ai/pricing |
| Fireworks dedicated GPU rates | https://www.morphllm.com/fireworks-ai-pricing |
| API provider speeds + blended price | https://artificialanalysis.ai/models/kimi-k3/providers |
| OpenRouter K3 pricing | https://openrouter.ai/moonshotai/kimi-k3 |
| Self-hosting cost analysis | https://northflank.com/blog/what-is-kimi-k3-self-hosting |
| Server pricing + OEM lead times | https://slyd.com/resources/oem-comparison |
| NVIDIA AI GPU pricing guide | https://intuitionlabs.ai/articles/nvidia-ai-gpu-pricing-guide |
| AI colocation pricing per kW | https://www.quotecolo.com/blog/colocation-2/ai-colocation-pricing-cost/ |
| Colocation cost per kW (corroboration) | https://encoradvisors.com/data-center-colocation-pricing/ |
| B300 cross-provider price comparison | https://getdeploying.com/gpus/nvidia-b300 |
| License analysis | https://www.unite.ai/moonshot-opens-kimi-k3-weights-under-a-revenue-tiered-license/ |
| SkyPilot Kimi-K2 multi-node example | https://docs.skypilot.ai/en/latest/examples/models/kimi-k2.html |
| Context / open-weights commentary | https://www.interconnects.ai/p/kimi-k3-the-open-weights-escalation |

### Confidence labels

- **Measured and sourced:** model card facts, `config.json` architecture values, GPU specs, all
  list prices, vLLM's published throughput on GB300 NVL72, colo $/kW ranges, OEM lead times.
- **Derived arithmetic:** every $/M-token figure, all TCO and break-even numbers, and the §1b
  cache-memory model (per-token MLA KV and per-sequence KDA state). Reproducible from the inputs
  above; the interactive page exposes every input.
- **Estimated, flagged as such:** 16× H200 throughput (§5.3), three-year residual value (§6.3),
  cold-start reload durations (§7). No published measurements exist for these.

> **Resolved 2026-07-28.** An earlier revision carried a guessed *"~20 KB/token KV"* figure scaled
> from DeepSeek-V3. It is now derived exactly from `config.json` (§1b): **13.5 KiB/token at fp8**,
> and the guess had also omitted the per-sequence KDA state entirely — which turns out to dominate
> memory below ~31k context. Two remaining soft spots in that model, both engine choices rather
> than model constants: the **state dtype** (fp32 vs bf16, exposed as a toggle) and the flat
> **8 GB/GPU runtime overhead** allowance, which is a rule of thumb, not a measurement.
