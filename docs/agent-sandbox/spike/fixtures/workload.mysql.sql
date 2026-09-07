-- Agent 沙盒 · ChangeSet A/B 路径对照实验的标准操作序列（MySQL 版）
--
-- 使用方式：
--   1. 在沙盒库上执行本脚本；
--   2. 分别用路径 A（块级差异翻译）与路径 B（binlog 解析）采集 ChangeSet；
--   3. 用 spike/compare_changesets.py 比对两份结果。
--
-- 每个 STEP 后的注释是**期望的 ChangeSet 语义**，人工核对时以此为准。

CREATE DATABASE IF NOT EXISTS app;
USE app;

DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
  id      BIGINT PRIMARY KEY,
  status  VARCHAR(32) NOT NULL,
  amount  DECIMAL(12,2) NOT NULL,
  note    TEXT NULL
) ENGINE=InnoDB;

DROP TABLE IF EXISTS no_pk_audit;
CREATE TABLE no_pk_audit (
  actor VARCHAR(64),
  action VARCHAR(64)
) ENGINE=InnoDB;

-- 基线数据：以下写入发生在打基线之前，不应出现在 ChangeSet 中。
INSERT INTO orders (id, status, amount, note) VALUES
  (1002, 'pending',   80.00, NULL),
  (1003, 'cancelled', 10.00, NULL),
  (1004, 'pending',   50.00, NULL),
  (1005, 'pending',   60.00, NULL);
INSERT INTO no_pk_audit (actor, action) VALUES ('seed', 'init');
COMMIT;

-- ===== 在此处创建沙盒基线（snapshot / baseline_time），以下才是待采集的变更 =====

-- STEP 1 单行 INSERT
-- 期望：orders 1 条 insert，after 完整，before = null
INSERT INTO orders (id, status, amount, note) VALUES (1001, 'pending', 120.50, NULL);
COMMIT;

-- STEP 2 单行 UPDATE，改 2 列
-- 期望：orders 1 条 update，changed_columns = [amount, status]
UPDATE orders SET status = 'shipped', amount = 88.00 WHERE id = 1002;
COMMIT;

-- STEP 3 单行 DELETE
-- 期望：orders 1 条 delete，before 镜像完整（块级路径的典型弱项）
DELETE FROM orders WHERE id = 1003;
COMMIT;

-- STEP 4 批量 UPDATE（先扩量到 500 行再批量改）
-- 期望：500 条 update，逐行 before/after 均可还原，且 integrity.completeness = complete
INSERT INTO orders (id, status, amount, note)
SELECT 2000 + n, 'pending', 1.00 * n, NULL
FROM (
  SELECT a.d + b.d * 10 + c.d * 100 AS n
  FROM (SELECT 0 d UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4
        UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) a,
       (SELECT 0 d UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4
        UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9) b,
       (SELECT 0 d UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4) c
) t;
COMMIT;
UPDATE orders SET status = 'bulk' WHERE id BETWEEN 2000 AND 2499;
COMMIT;

-- STEP 5 同一行连续修改 3 次
-- 期望：与合并回切语义一致即可，但 A/B 两条路径必须给出**相同**的表示。
--       若采用「折叠为净变更」，结果应为 1 条 update：pending -> final。
--       若采用「保留操作序列」，结果应为 3 条 update，且顺序一致。
--       本项的实际结论需写入 02 文档第 6 节。
UPDATE orders SET status = 'step5-a' WHERE id = 1004;
UPDATE orders SET status = 'step5-b' WHERE id = 1004;
UPDATE orders SET status = 'final'   WHERE id = 1004;
COMMIT;

-- STEP 6 同一事务内插入后删除
-- 期望：净效果为零，ChangeSet 中**不应**出现 id=1006 的任何行
START TRANSACTION;
INSERT INTO orders (id, status, amount, note) VALUES (1006, 'temp', 1.00, NULL);
DELETE FROM orders WHERE id = 1006;
COMMIT;

-- STEP 7 回滚的事务
-- 期望：ChangeSet 中**不应**出现 id=1005 的变更（块级路径的高风险点）
START TRANSACTION;
UPDATE orders SET status = 'should-not-appear' WHERE id = 1005;
ROLLBACK;

-- STEP 8 DDL
-- 期望：ddl_changes 1 条，ddl_type = alter_table，risk = high，门禁默认拒绝合并
ALTER TABLE orders ADD COLUMN memo VARCHAR(200) NULL;

-- STEP 9 无主键表的 UPDATE
-- 期望：该表的 primary_key = []，integrity.completeness = unknown，门禁拒绝
UPDATE no_pk_audit SET action = 'changed' WHERE actor = 'seed';
COMMIT;

-- STEP 10 大字段更新
-- 期望：按边界 5.4，只记录变更标志与长度；A/B 两条路径行为必须一致
UPDATE orders SET note = REPEAT('x', 100000) WHERE id = 1001;
COMMIT;

-- ===== 采集终点 =====
