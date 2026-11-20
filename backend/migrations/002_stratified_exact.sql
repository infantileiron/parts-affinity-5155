-- 分层精确检验（stratified-exact-max3-v2）表结构增量
-- 对应 backend/src/config/schema.py 启动时的轻量补齐；此处保存完整 DDL。

ALTER TABLE `mining_runs`
  ADD COLUMN `alpha` float DEFAULT 0.05 AFTER `min_lift`;

ALTER TABLE `association_rules`
  ADD COLUMN `expected` float DEFAULT NULL AFTER `count`,
  ADD COLUMN `p_value` float DEFAULT NULL AFTER `expected`,
  ADD COLUMN `q_value` float DEFAULT NULL AFTER `p_value`,
  ADD COLUMN `in_scope` tinyint(1) DEFAULT NULL AFTER `q_value`,
  ADD COLUMN `qualified` tinyint(1) DEFAULT 0 AFTER `in_scope`,
  ADD COLUMN `reasons` json DEFAULT NULL AFTER `qualified`,
  ADD COLUMN `per_equipment` json DEFAULT NULL AFTER `reasons`;

ALTER TABLE `kits`
  ADD COLUMN `evidence` json DEFAULT NULL AFTER `status`;

-- 历史批次：candidates 家族只保存在 association_rules 中，旧批次缺少
-- p_value/per_equipment/universe；首次调整阈值或起草时会按冻结样本补齐，
-- 不会新增采样，也不会改变多重校正范围。
