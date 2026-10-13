"""SQL字段权限、作用域和资源边界回归（不调用模型）。"""
import pytest

from agent_core.sql_safety import SQLValidationError, validate_and_limit_sql

SAFE = [
    "SELECT 1", "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(DISTINCT order_id) FROM orders",
    "SELECT COUNT(DISTINCT user_name) FROM orders",
    "SELECT SUM(payment_amount) FROM orders",
    "SELECT platform_type, SUM(payment_amount) AS sales FROM orders GROUP BY platform_type ORDER BY sales DESC",
    "SELECT ROUND(SUM(payment_amount)/NULLIF(COUNT(DISTINCT order_id),0),2) FROM orders",
    "SELECT SUM(CASE WHEN is_refund='是' THEN payment_amount ELSE 0 END) FROM orders",
    "SELECT order_date,COUNT(*) FROM orders WHERE order_date>='2026-01-01' GROUP BY order_date",
    "WITH t AS (SELECT SUM(payment_amount) AS n FROM orders) SELECT n FROM t",
    "WITH t AS (SELECT payment_amount FROM orders), x AS (SELECT SUM(payment_amount) AS n FROM t) SELECT n FROM x",
    "SELECT COUNT(*) FROM (SELECT COUNT(*) AS n FROM orders GROUP BY user_name HAVING COUNT(*)>=2) t",
    "SELECT SUM(payment_amount) FROM orders UNION ALL SELECT AVG(payment_amount) FROM orders",
    "SELECT product_id FROM orders WHERE payment_amount>(SELECT AVG(payment_amount) FROM orders)",
    "SELECT DATE_FORMAT(order_date,'%Y-%m'),SUM(payment_amount) FROM orders GROUP BY DATE_FORMAT(order_date,'%Y-%m')",
    "SELECT product_id FROM orders LIMIT 10 OFFSET 5",
    "SELECT a.product_id FROM orders a JOIN orders b ON a.product_id=b.product_id LIMIT 2",
]
UNSAFE = [
    "SELECT * FROM orders", "SELECT o.* FROM orders o",
    "SELECT user_name FROM orders", "SELECT order_id AS anonymous FROM orders",
    "SELECT order_seq_id FROM orders", "SELECT MIN(user_name) FROM orders",
    "SELECT COUNT(user_name) FROM orders", "SELECT COUNT(DISTINCT LOWER(user_name)) FROM orders",
    "SELECT COUNT(*) FROM orders WHERE user_name='secret'",
    "SELECT user_name,COUNT(*) FROM orders GROUP BY user_name",
    "WITH t AS (SELECT user_name AS x FROM orders) SELECT x FROM t",
    "SELECT product_id FROM orders UNION SELECT user_name FROM orders",
    "SELECT product_id FROM orders WHERE product_id IN (SELECT user_name FROM orders)",
    "SELECT email FROM orders", "SELECT * FROM mysql.user",
    "SELECT COUNT(*) FROM otherdb.orders", "SELECT COUNT(*) FROM secrets",
    "WITH secrets AS (SELECT COUNT(*) AS n FROM orders) SELECT COUNT(*) FROM secrets UNION SELECT COUNT(*) FROM mysql.secrets",
    "SELECT SLEEP(1)", "SELECT BENCHMARK(10,1)", "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT GET_LOCK('x',1)", "SELECT custom_function(payment_amount) FROM orders",
    "SELECT 1 INTO OUTFILE '/tmp/test'", "SELECT 1; SELECT 2",
    "SELECT product_id FROM orders LIMIT ?", "SELECT product_id FROM orders LIMIT -1",
    "SELECT product_id FROM orders LIMIT 1 OFFSET 10001", "SELECT @secret",
    "SELECT @@version", "SELECT /* comment */ 1", "SELECT /*!50000 SLEEP(1) */ 1",
    "WITH RECURSIVE t AS (SELECT 1) SELECT 1 FROM t",
    "SELECT a.product_id FROM orders a CROSS JOIN orders b",
    "SELECT a.product_id FROM orders a JOIN orders b ON 1=1",
    "SELECT a.product_id FROM orders a JOIN orders b ON a.product_id=a.product_id",
    "SELECT a.product_id FROM orders a JOIN orders b ON a.product_id=b.product_id OR 1=1",
    "SELECT payment_amount FROM orders FOR UPDATE",
]


@pytest.mark.parametrize("sql", SAFE)
def test_safe_aggregates_and_scopes(sql):
    assert "LIMIT" in validate_and_limit_sql(sql)


@pytest.mark.parametrize("sql", UNSAFE)
def test_unproved_access_is_rejected(sql):
    with pytest.raises(SQLValidationError):
        validate_and_limit_sql(sql)
