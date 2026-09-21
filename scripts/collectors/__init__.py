"""采集器运维脚本（smoke / dry-run）。

smoke：真实调用验证连通性与字段（不落库）。
dry-run：跑完整采集流程但 savepoint 回滚（不落库），验证幂等与故障隔离。
"""
