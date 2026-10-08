-- EMANAGER CRM (Supabase project EmanagerCRM), OPTIONAL, applied by hand in the SQL Editor.
--
-- Lets EMANAGER Dialer show the client card from the first second of a call: returns the
-- number of the call that just started, from the Zadarma webhook events the CRM already
-- stores in webhook_events_queue (that table stays closed to everyone; only this function
-- reads it, and only for CRM staff).
--
-- Read-only: changes no table, no data and no existing policy.
-- Undo: drop function public.dialer_current_call(text, integer);
--
-- Zadarma events used:
--   NOTIFY_OUT_START  outbound call dialled: destination = client, internal = SIP
--   NOTIFY_ANSWER     call answered: caller_id = client (inbound) or the SIP (outbound)
--   NOTIFY_START      inbound call ringing (no SIP yet): caller_id = client

create or replace function public.dialer_current_call(_internal text default null, _within_seconds integer default 120)
returns table (phone text, direction text, internal text, pbx_call_id text, event_at timestamptz)
language sql
stable
security definer
set search_path = public
as $$
  select
    case
      when e.event_type = 'NOTIFY_OUT_START' then e.payload->>'destination'
      when length(regexp_replace(coalesce(e.payload->>'caller_id', ''), '\D', '', 'g')) >= 9 then e.payload->>'caller_id'
      else e.payload->>'destination'
    end as phone,
    case
      when e.event_type = 'NOTIFY_OUT_START' then 'outbound'
      when length(regexp_replace(coalesce(e.payload->>'caller_id', ''), '\D', '', 'g')) >= 9 then 'inbound'
      else 'outbound'
    end as direction,
    e.payload->>'internal' as internal,
    e.payload->>'pbx_call_id' as pbx_call_id,
    e.created_at as event_at
  from public.webhook_events_queue e
  where public.is_any_staff(auth.uid())
    and e.event_type in ('NOTIFY_OUT_START', 'NOTIFY_ANSWER', 'NOTIFY_START')
    and e.created_at > now() - make_interval(secs => least(greatest(_within_seconds, 10), 600))
    and (_internal is null or e.payload->>'internal' = _internal or e.event_type = 'NOTIFY_START')
  order by e.created_at desc
  limit 1;
$$;

revoke all on function public.dialer_current_call(text, integer) from public, anon;
grant execute on function public.dialer_current_call(text, integer) to authenticated;
