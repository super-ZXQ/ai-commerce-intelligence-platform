#!/bin/sh
set -eu
# 空库建表完成后才可授予列级权限；不授予账本payload、元数据或其他表。
case "${DB_NAME}" in *[!A-Za-z0-9_]*|'') exit 1 ;; esac
mysql --protocol=TCP -h mysql -uroot <<SQL
GRANT SELECT (
  order_seq_id, order_id, user_name, product_id, order_amount, payment_amount,
  channel_id, platform_type, order_time, payment_time, is_refund, discount_amount,
  payment_duration_sec, order_date, order_hour, weekday, order_status, event_version, updated_at
) ON \`${DB_NAME}\`.orders TO 'ea_ai'@'%';
SQL
