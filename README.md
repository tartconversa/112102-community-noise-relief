# 社区噪声证据与分时整改承诺

社区夜间监测、设施整改提案和签收的合成资料。 所有标识、金额、地名和观察值均为合成数据。

## 来源资料

- `sources/monitor_locations.csv`
- `sources/readings.csv`
- `sources/operator_nights.csv`
- `sources/measurement_revisions.csv`
- `sources/beneficiaries.csv`
- `sources/remedy_offers.csv`
- `sources/remedy_events.csv`
- `sources/budget.csv`

一晚对应一个固定观察窗。observed_db 与 background_db 是同一时间窗的等效声级分贝，合并需要在线性能量尺度解释；background_db 来自同区域独立道路背景源。offline 时对应站点没有该晚实测值。operator_nights 的范围端点均包含在内，nights_excluded 是有运营安排但未出现的夜晚。

居民权重以楼栋为粒度，同一区可能出现多个监测点。eligibility_cutoff_db 是各楼栋议定的评估边界。

整改提案成本和预算以分表示。night_reduction 的 units 是覆盖目标区的减扰夜晚数，多个提案可涉及同一夜晚。隔声设施的 units 是需验收楼栋份额。签署与部分验收各有独立回执。

## 本地检查

使用 Python 3.14，仅需标准库。在项目目录执行以下命令检查输入表结构、事件引用和基础记录覆盖。

```sh
python -m unittest discover -s tests -v
```
