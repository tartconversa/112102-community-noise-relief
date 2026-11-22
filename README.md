# 社区噪声证据与分时整改承诺

社区夜间监测、设施整改提案和签收的合成资料，附一套本地分析应用。所有标识、金额、
地名和观察值均为合成数据。

应用把三件事**分开**说明，因为它们回答不同的问题：

1. **观测到的夜间情况** —— 哪些夜晚、哪些监测点真的测到了，覆盖是否完整。
2. **能归因于试营的额外扰动** —— 扣掉独立道路背景、并用对照子区校正后的增量。
3. **数据不足的范围** —— 哪些夜次、楼栋和片区无法支撑结论，缺什么、要补测什么。

同时它把整改从一个时点扩展到一条时间线：登记校正、提预算方案、签署、取消、验收，
并在校正改变结论后解释当前可用预算、尚欠整改量，以及各楼栋的证据。

## 来源资料

- `sources/monitor_locations.csv`
- `sources/readings.csv`
- `sources/operator_nights.csv`
- `sources/measurement_revisions.csv`
- `sources/beneficiaries.csv`
- `sources/remedy_offers.csv`
- `sources/remedy_events.csv`
- `sources/budget.csv`

一晚对应一个固定观察窗。`observed_db` 与 `background_db` 是同一时间窗的等效声级分贝，
合并需要在线性能量尺度解释；`background_db` 来自同区域独立道路背景源。`offline`
时对应站点没有该晚实测值。`operator_nights` 的范围端点均包含在内，`nights_excluded`
是有运营安排但未出现的夜晚。

居民权重以楼栋为粒度，同一区可能出现多个监测点。`eligibility_cutoff_db` 是各楼栋议定
的评估边界。

整改提案成本和预算以分表示。`night_reduction` 的 units 是覆盖目标区的减扰夜晚数，多个
提案可涉及同一夜晚。隔声设施的 units 是需验收楼栋份额。签署与部分验收各有独立回执。

## 运行

使用 Python 3.14，仅需标准库。在项目目录执行：

```sh
python -m unittest discover -s tests -v      # 输入契约 + 业务规则检查
python -m noise_project evidence --asof 21   # 重算监测期证据
python -m noise_project plan                 # 提交整改预算方案
```

状态默认写入 `.noise_state/state.json`（可用 `--state` 指定）。`--sources` 可指向另一
份同结构的合成数据，因此“同一个本地项目”可以随时重算。

## 证据口径（都记录在报告摘要里，可回查）

- **夜间窗口**：试营期 = 各运营方排期并集减去未出现夜次；基线 = 试营首夜之前的观测夜。
- **能量尺度**：`observed` 与道路 `background` 相减得到残余声级
  `10·log10(10^(obs/10) − 10^(bg/10))`；跨夜聚合先平均残余**能量**再转 dB，避免逐夜
  相减后的算术平均失真。
- **可比背景**：某监测点试营期与基线期背景中位数相差超过 1 dB 即视为前后不可比，该点
  不参与增量估计。
- **对照子区**：比较点若受道路施工影响、或背景漂移、或自身变化超过 2 dB，就不能作为
  干净对照。只有存在干净对照时，区域的“额外扰动”才标记为 `validated`；否则标为
  `confounded`，并写明是哪一个对照点拖累。
- **多运营方片区**：额外声级按**能量均分**给出每方份额（标注为约定口径，非实测拆分）。
  人口也按“听到的运营方”均分，绝不把整片人口整份记给每一方，也不拿投诉条数替代暴露。
- **单点夜 vs 确证夜**：某夜只有 1 个暴露点测到并超边界 → 仅记为 `single_sensor`，
  不能确证；≥2 个点同时超边界才算 `corroborated`。楼栋需达到“最少确证夜数”（默认 3）
  才算通过资格边界。
- **校正生效日**：同一监测点/夜次有多条校正时，取 `learned_day ≤ as-of` 中最新的；尚未
  学习到的校正记为 `pending`，在证据里提示“该结论会在校正生效后改变”。

## 应用能做的完整闭环

```sh
# 1) 采用会议口径（快照可回查）
python -m noise_project evidence --asof 20 --adopt "9月协商会初稿"
python -m noise_project evidence --asof 21 --adopt "9月协商会定稿"

# 2) 登记一条后到的校正，指定生效日
python -m noise_project correct M4 16 54 --learned-day 25 --reason repair

# 3) 对照上次采用的快照提方案；校正跨过资格边界时会列出
python -m noise_project plan --asof 25 --since-snapshot S002

# 4) 签署 / 申请取消 / 确认取消 / 验收 / 预算调整
python -m noise_project sign A1 --day 19
python -m noise_project cancel A1 --day 25 --reason "换方案"
python -m noise_project confirm-cancel A1 --day 26
python -m noise_project accept A3 --building L2 --units 0.5 --day 24
python -m noise_project budget Z2 --delta -500 --day 26 --reason "缩减"

# 5) 回查已采用口径与全部操作
python -m noise_project snapshots
python -m noise_project show S002
python -m noise_project audit
```

## 整改规则

- **签了就算数**：已签承诺在取消被**确认**之前一直保留资金；只提出取消、未确认，钱不能
  释放给别的提案。
- **一个夜晚只抵扣一份**：`night_reduction` 的同一夜不能同时冲抵两个运营方的额度，即使
  两家的生效区间重叠。
- **一栋楼只认一次**：同一栋楼的隔声份额不能被两个运营方都申报为“已验收”；同一运营方
  可以补足自己的剩余份额。
- **验收只认所指范围**：一张施工回执只把**该楼栋、该份额**记为已交付，不会把整片区域
  记为已改善；片区“尚欠整改量”按未交付份额折算金额。
- **名额与预算**：隔声每栋占用一个验收名额；新提案只有在楼栋当前证据通过边界时才可批，
  否则留待复测。
- **跨边界**：校正把某楼栋从“已验证”推到“未验证”时，**新提案暂停**，**已签承诺仍须
  履行**；预算在取消确认前不释放。方案会逐栋列出暂停的提案与仍有效的承诺。

## 代码结构

- `noise_project/sources.py` —— 读取并校验全部来源表（引用、枚举、数值范围）。
- `noise_project/analysis.py` —— 证据引擎（覆盖 / 归因 / 逐栋）+ 预算整改规划器。
- `noise_project/store.py` —— 本地追加式台账（校正、签署、取消、验收、预算、快照）。
- `noise_project/render.py` —— 面向会议的中文文本报告。
- `noise_project/cli.py` —— 命令行入口。

`tests/test_sources.py` 检查输入契约；`tests/test_analysis.py` 覆盖上述业务规则。
