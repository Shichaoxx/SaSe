# Qwen3.8-27B 的本地分页评估

**在与校准集分开的九个配对任务上，Native、Fixed 和 Gated 各成功 4/9。** 三组共 27 份最终答案全部准确返回了目标 ID。严格得分的差异出在实际读取了哪些页。Fixed 在三个单页任务上优于 Native，又在三个只要首页的任务上落后于 Native。Gated 的逐题成败与 Native 全部相同。[逐次结果与运行配置](data/local-27b-pagination.json)

## Native 校准

使用种子 7301 生成 `basic` 任务，每类三个，严格成功合计 5/9。每次由真实 Pi 1.6.0 调用本地模型和分页工具，使用独立会话。校准仅运行 Native。严格成功要求读齐目标页、遵守读取范围，并恰好返回全部目标 ID 各一次。九次运行均自然结束，评分完整。

| 任务 | 需要读取的页 | 最终 JSON 和 ID 正确 | 严格成功 | 失败的工具访问序列 |
| --- | --- | ---: | ---: | --- |
| 获取全部，首页有下一页 | 1、2 | 3/3 | 1/3 | 两次读完第 2 页后尝试读取第 3 页 |
| 只取首页，仍有下一页 | 1 | 3/3 | 3/3 | 三次均只访问第 1 页 |
| 获取全部，首页即末页 | 1 | 3/3 | 1/3 | 两次读完第 1 页后尝试读取第 2 页 |
| 合计 | | 9/9 | 5/9 | 四次越界尝试 |

“只取首页”的三次运行均重复读取了第 1 页。重复读取范围内的有效页计作额外工具调用，仍可严格成功。读取一个实际存在但超出用户目标的页，或尝试读取一个不存在的页，均为严格失败。本批四次失分都属于后一种情况。

## 新数据上的三组比较

配对评估使用种子 8147 生成三组新数据。每组数据各构造三种任务，共九题。同组任务共享首页记录。每题在 Native、Fixed 和 Gated 上各运行一次，共 27 次。三组共享模型、任务、工具、提示格式和预算，任务内运行顺序由种子生成。Native 使用宿主自身能力。Fixed 在成功读取首页后提供冻结的 `paginate`，Gated 在同一个机会结合目标与 `has_next` 决定是否提供它。配对差值使用种子 8147 下同题的 Native 结果。[分页评估协议](evaluation.md)

### 严格成功与实际页访问

| 任务 | Native | Fixed | Gated | 实际读取 |
| --- | ---: | ---: | ---: | --- |
| 获取全部，首页有下一页 | 1/3 | 1/3 | 1/3 | 同一题三组都读到第 2 页后停止，另外两题三组都多尝试了不存在的第 3 页 |
| 只取首页，仍有下一页 | 3/3 | 0/3 | 3/3 | Native 和 Gated 重读首页。Fixed 三次都读取了存在但超出用户目标的第 2 页 |
| 获取全部，首页即末页 | 0/3 | 3/3 | 0/3 | Native 和 Gated 都尝试了不存在的第 2 页。Fixed 三次都重读了有效的第 1 页 |
| 合计 | **4/9** | **4/9** | **4/9** | 27 次运行全部自然结束，评分与用量均完整 |

按同题逐一比较，Fixed 相对 Native 为 3 胜、3 平、3 负。Gated 相对 Native 为 9 平，相对 Fixed 为 3 胜、3 平、3 负。三组都读齐了全部必需页。只取首页的三次 Fixed 运行即使多读了第 2 页，最终答案仍只列出用户要求的首页 ID。

### 模型用量与技能曝光

| 策略 | 模型调用合计 | 工具调用合计 | 技能分配 / 请求侧观察到 | 总 token | 每任务较 Native 的平均 token 差 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native | 29 | 20 | 0 / 0 | 14,825 | 基线 |
| Fixed | 29 | 20 | 9 / 9 | 16,241 | +157.33 |
| Gated | 29 | 20 | 3 / 3 | 15,221 | +44.00 |

每组都有 8 次额外工具调用，计数包含重复读取、超目标读取和无效页尝试。Gated 只在要求读取两页的三题分配了技能。全部 12 次技能分配均有上下文投递回执和 `request_payload` 观察回执，说明适配器在组装请求时看到完整提示。每个策略与 Native 的 token 差均来自九个完整配对，包含模型服务报告的输入、输出及缓存用量。

本次配对每类仅三题，每题每种策略各一次。样本中同时出现 Fixed 的收益与负迁移，三组总体得分持平。每类任务的真实收益、生成波动及不同量化配置下的表现，需要更多独立任务和重复运行估计。

## 离线复核成绩

在仓库根目录运行以下命令，用 Python 标准库和项目评分器从公开观测重算成功次数。校准集和配对集分别按各自的种子与计划任务计数。

```sh
python3 - <<'PYTHON'
import json
from pathlib import Path
from sase.evaluation import summarize

evidence = json.loads(Path("docs/data/local-27b-pagination.json").read_text())
for batch in evidence["batches"]:
    manifest = batch["manifest"]
    summary = summarize(manifest["tasks"], batch["observations"], manifest["arms"])
    for row in summary["groups"]:
        if row["task_type"] == "overall":
            print(manifest["seed"], row["arm"], f"{row['successes']}/{row['planned']}")
PYTHON
```

## 模型、推理与复现

- 模型为 Qwen3.8-27B，权重使用 Unsloth 的 `UD-IQ3_XXS` GGUF。模型与量化仓库标注 Apache-2.0 许可。GGUF 大小为 10,934,860,704 字节，SHA-256 为 `c0b7c3038681ed2e3040456c1dd45f9858b6c2290bed172c70388a94874f3eee`。[原始模型](https://huggingface.co/Qwen/Qwen3.8-27B) · [固定版本量化权重](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/blob/313447f257f7ebde0b968e4778feef774546ed81/Qwen3.8-27B-UD-IQ3_XXS.gguf)
- 推理引擎为 llama.cpp `b12823`，采用 MIT 许可。运行使用 macOS arm64 发行包，归档 SHA-256 为 `709005367a1fa0e3bc025355a956b8cdb0e9eeea70f70ed9e9c5b1a04fff483a`。[固定引擎发行版](https://github.com/ggml-org/llama.cpp/releases/tag/b12823) · [引擎许可证](https://github.com/ggml-org/llama.cpp/blob/b12823/LICENSE)
- 两批均使用 `basic` 任务，每次最多 6 次模型调用、240 秒。服务端温度为 0，采样种子为 42，关闭思考模式。服务端采样种子与用于生成任务的两个种子各自独立。结果描述此模型、量化、工具模板和推理配置的联合表现，实验设计无法单独估计参数规模的效应。

将固定版本的权重放在当前目录，用对应版本的 `llama-server` 启动服务。示例使用回环端口 8080。

```sh
llama-server -m Qwen3.8-27B-UD-IQ3_XXS.gguf --alias qwen3.8-27b-ud-iq3_xxs \
  --host 127.0.0.1 --port 8080 --cors-origins localhost --no-webui --jinja \
  --reasoning off --chat-template-kwargs '{"enable_thinking":false}' --temp 0 --seed 42 \
  --parallel 1 --ctx-size 4096 --predict 512 --threads 4 --threads-batch 4 \
  --batch-size 128 --ubatch-size 64 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
  --cache-ram 0 --cache-prompt
```

在已执行 `npm ci` 的 SaSe 仓库中，依次运行校准与独立配对评估。每条命令会创建独立的证据目录。

```sh
npm run evaluate -- --calibrate-native --model qwen3.8-27b-ud-iq3_xxs \
  --base-url http://127.0.0.1:8080/v1 --task-profile basic --seed 7301 \
  --per-type 3 --max-model-calls 6 --timeout 240

npm run evaluate -- --model qwen3.8-27b-ud-iq3_xxs \
  --base-url http://127.0.0.1:8080/v1 --task-profile basic --seed 8147 \
  --per-type 3 --max-model-calls 6 --timeout 240 --skill examples/paginate/SKILL.md
```
