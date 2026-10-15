-- Fails for orders whose total differs from the sum of their line amounts.
select
    o.order_id,
    o.order_amount,
    sum(i.line_amount) as items_amount
from {{ ref('stg_orders') }} as o
join {{ ref('stg_order_items') }} as i on i.order_id = o.order_id
group by o.order_id, o.order_amount
having abs(o.order_amount - sum(i.line_amount)) > 0.01
