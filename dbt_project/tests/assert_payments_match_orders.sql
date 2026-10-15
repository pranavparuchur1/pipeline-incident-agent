-- Fails for non-cancelled orders with no payment, or a payment total that differs from the order amount.
select
    o.order_id,
    o.order_amount,
    p.payment_amount
from {{ ref('stg_orders') }} as o
left join (
    select order_id, sum(amount) as payment_amount
    from {{ ref('stg_payments') }}
    group by order_id
) as p on p.order_id = o.order_id
where o.status <> 'cancelled'
  and (p.order_id is null or abs(p.payment_amount - o.order_amount) > 0.01)
