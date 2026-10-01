-- 0025 · email as a channel (spec 28.25): a customer's email becomes an inquiry, answered under the owner's approval
-- Mail reaches the platform through the provider's inbound webhook (Postmark), addressed to the business's inbound
-- address; that address is the channel's external id and routes the event like a phone number id does (0010
-- webhook_route). One spelling per address (lower case, no display name), so routing is an exact match.
-- The reply is an ordinary reply:send proposal: the same 20-hour decision window, the same standing approvals.
alter type app.inquiry_source add value if not exists 'email';          -- outside the transaction, as 0024

begin;

alter table app.channel_accounts add constraint channel_accounts_email_address
  check (kind::text <> 'email' or (external_id ~ '^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$' and length(external_id) <= 254));

commit;
