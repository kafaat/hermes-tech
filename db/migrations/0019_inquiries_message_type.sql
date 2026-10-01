-- 0019 · the type of an inbound message, so the owner knows a voice note is waiting in WhatsApp
-- Many customers in Yemen send voice notes. The worker read text only: a voice note, a photo or a location arrived as
-- an empty inquiry and was escalated as "no approved answer", and the owner saw an empty card in the portal. The
-- inquiry now records the message type (WhatsApp Cloud API types; anything else is 'unsupported'); the worker routes
-- on the text or the caption when there is one, escalates "non_text:<type>" when there is none, and ignores reactions.
-- The content of media is never fetched or stored here: the owner opens it in WhatsApp.
begin;

alter table app.inquiries
  add column message_type text not null default 'text'
    check (message_type in ('text', 'button', 'interactive', 'audio', 'image', 'video', 'document', 'sticker',
                            'location', 'contacts', 'unsupported'));

commit;
