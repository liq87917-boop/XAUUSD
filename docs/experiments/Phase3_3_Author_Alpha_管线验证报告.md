# 作者技能快照管线报告

> 本次仅验证管线正确性，不作任何真实作者技能结论。

- 评估时点 as_of：2026-09-05T04:00:00+00:00
- timing 阈值：0.001（10 bps ≈ 黄金典型点差+滑点量级）
- author_skill_snapshots：新增 0 / 累计 4（幂等已提交）

## 1. Mock 层（SYNTHETIC，仅验证计算逻辑）

| 作者 | 可评价样本 | direction_skill_raw | direction_skill | timing_skill_raw | timing_skill | calibration_score | ready |
|---|---:|---:|---:|---:|---:|---:|---|
| 金十数据快讯[MOCK] | 97 | 0.4021 | 0.4040 | 0.002817 | 0.7179 | 0.5343 | True |
| 新浪财经黄金频道[MOCK] | 37 | 0.5135 | 0.5128 | NOT_EVALUATED | NOT_EVALUATED | 0.6326 | True |
| 投研社-黄金策略组[MOCK] | 48 | 0.5000 | 0.5000 | NOT_EVALUATED | NOT_EVALUATED | 0.6305 | True |
| 宏观速递-FOMC观察[MOCK] | 37 | 0.4865 | 0.4872 | NOT_EVALUATED | NOT_EVALUATED | 0.6370 | True |

## 2. 真实层（待合法授权数据）

| 作者 | 观点数 | 可用标签 |
|---|---:|---:|
| 华尔街见闻 | 9 | 0 |
| 汇通网 | 2 | 0 |

> 真实层观点均为 UNTRUSTED_COLLECTION_TIME（输入缺独立 collected_at），
> forward-return-v2 在查询行情前直接拒绝，0 可用标签，不参与任何技能结论。
