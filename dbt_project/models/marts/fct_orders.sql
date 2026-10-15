with items as (
    select
        order_id,
        count(*) as item_count,
        sum(line_amount) as items_amount
    from {{ ref('stg_order_items') }}
    group by order_id
),

payments as (
    select
        order_id,
        sum(amount) as payment_amount,
        min(method) as payment_method
    from {{ ref('stg_payments') }}
    group by order_id
)

select
    o.order_id,
    o.customer_id,
    o.ordered_at,
    o.status,
    o.channel,
    o.currency,
    o.order_amount,
    o.partition_date,
    coalesce(i.item_count, 0) as item_count,
    i.items_amount,
    p.payment_amount,
    p.payment_method,
    p.order_id is not null as is_paid
from {{ ref('stg_orders') }} as o
left join items as i on i.order_id = o.order_id
left join payments as p on p.order_id = o.order_id
