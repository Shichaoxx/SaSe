# Qwen 分页配对试验

Pi 1.6.0 在三个合成任务上各运行 Native、Fixed 和 Gated，一共九次。按预先设置的评分规则，三组均为 **0/3**。每次运行都收到了模型答复，九份答复均未解析为要求的 `{"ids":[...]}`。三组在工具调用成本上出现差异，主要来自“只取首页”的任务。[评分方法](evaluation.md) · [任务与逐次观测](data/pagination-pilot.json)

| 任务 | Native 工具调用 / 总 token | Fixed 工具调用 / 总 token | Gated 工具调用 / 总 token |
| --- | ---: | ---: | ---: |
| 全部记录，有下一页 | 86 / 197,616 | 86 / 197,745 | 86 / 197,744 |
| 只取首页，有下一页 | 1 / 723 | 85 / 193,916 | 1 / 723 |
| 全部记录，已到末页 | 1 / 705 | 2 / 1,203 | 1 / 705 |

工具调用包含访问无效页的尝试。Token 采用 Pi 从服务端得到的总量，包含缓存命中，不能直接换算为费用。每个格子只有一次运行。模型调用逐次累计上下文 token，因此连续的工具调用会显著放大总量。

**首要观察来自“只取首页”。** 三组拿到相同的第一页，内容中均有下一页游标。Native 和 Gated 各读取一次第一页，Gated 记录了 `first_page_only`。Fixed 的一次技能注入得到请求侧曝光回执，模型随后读取了超出用户范围的第 2 页，并尝试访问不存在的第 3 至 85 页。相反，在“获取全部记录且有下一页”任务中，三组都出现了连续读取无效页的行为。

三个完整配对中，Gated 相对 Native 的成功率差为 0 个百分点，每任务平均多 42.7 个总 token。Fixed 相对 Native 平均多 64,606.7 个总 token。小批数据展示了技能投递、行为和成本之间的可观测差异。对其他任务、模型和技能的外推需要独立样本。

## 复核

- 模型为 Qwen2.5-1.5B-Instruct 的 Q4_K_M GGUF，使用 llama.cpp b12726 本地推理。上下文 8192，单槽，启用 Jinja 工具模板，温度 0，服务端默认生成上限 512。权重 SHA-256 及运行源码指纹随数据提供。[模型发布页](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF) · [推理引擎版本](https://github.com/ggml-org/llama.cpp/releases/tag/b12726)
- 任务种子为 42，每类一个任务，每组使用新 Pi 会话。任务内的策略顺序提前随机化。Fixed 和 Gated 在同一个首页边界至多分配一次相同的批准技能。Fixed 三次分配与 Gated 一次分配均有 `request_payload` 曝光记录。
- 公开 JSON 包含任务定义、计划顺序、实际页访问、模型用量与首页决策。独立的格式复查在相同模型上重跑一次 Native 首页任务，得到一次工具调用；该答复中也未找到包含 `ids` 数组的 JSON 对象。这次诊断单列在 `diagnostic` 中，九次主试验的分母保持不变。

在仓库根目录可从公开观测重新计算报告。

```sh
python3 - <<'PY'
import json
from pathlib import Path
from sase.evaluation import report, summarize

batch = json.loads(Path("docs/data/pagination-pilot.json").read_text())
print(report(summarize(batch["manifest"]["tasks"], batch["observations"])))
PY
```
