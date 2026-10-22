from .models import Batch, InboundEvent
import hashlib

def _fingerprint(value):
    # A missing identity is not a stable identity.  Keep it empty so the
    # receiver can fail closed instead of accidentally treating a hash of
    # ``None``/``''`` as a bindable contact.
    if value is None or str(value) == '':
        return ''
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:12]

def _text(item):
    if isinstance(item.get('text'), str) and item['text'].strip():
        return item['text']
    entries = item.get('item_list')
    if not isinstance(entries, list) or len(entries) != 1:
        return ''
    if not isinstance(entries[0], dict):
        return ''
    text_item = entries[0].get('text_item') or {}
    if isinstance(text_item, dict) and isinstance(text_item.get('text'), str) and text_item['text'].strip():
        return text_item['text']
    return ''


_MEDIA_ENTRY_KEYS = {
    'image_item', 'voice_item', 'video_item', 'file_item', 'link_item',
    'refer_item', 'reference_item', 'location_item', 'transfer_item',
    'red_packet_item', 'appmsg_item', 'mini_program_item', 'music_item',
    'sticker_item', 'emoji_item', 'merged_msg_item', 'calendar_item',
    'pat_item', 'system_item', 'revoke_item', 'finder_item',
    'chat_history_item', 'post_item', 'business_item', 'weapp_item',
    'video_chat_item', 'tool_call_item', 'tool_call_start_item',
    'tool_call_result_item',
}


def _is_plain_text(item):
    """Accept one text item with arbitrary protocol metadata, but no media."""
    if isinstance(item.get('text'), str) and bool(item.get('text').strip()):
        return not any(
            key in item
            for key in (
                'item_list', 'image_item', 'voice_item', 'video_item', 'file_item',
                'link_item', 'refer_item', 'location_item', 'transfer_item',
                'red_packet_item', 'appmsg_item', 'mini_program_item',
            )
        )
    entries = item.get('item_list')
    if not isinstance(entries, list) or len(entries) != 1:
        return False
    entry = entries[0]
    if not isinstance(entry, dict):
        return False
    if any(key in _MEDIA_ENTRY_KEYS or (isinstance(key, str) and key.endswith('_item') and key != 'text_item') for key in entry):
        return False
    text_item = entry.get('text_item')
    return isinstance(text_item, dict) and isinstance(text_item.get('text'), str) and bool(text_item['text'].strip())

def parse_batch(raw, account_scope='default'):
    msgs=[]
    for item in raw.get('msgs',[]):
        message_id = item.get('message_id') if isinstance(item, dict) else None
        if message_id is None or str(message_id) == '':
            # A cursor can still advance past a malformed server item; no
            # business event is emitted without a stable idempotency key.
            continue
        text = _text(item) if _is_plain_text(item) else ''
        msgs.append(InboundEvent(account_scope, str(message_id), _fingerprint(item.get('from_user_id')), item.get('received_at',''), text, _fingerprint(item.get('to_user_id')), item.get('conversation_type','one_to_one'), item.get('context_token'), item.get('from_user_id'), bool(item.get('is_forwarded') or item.get('forwarded'))))
    return Batch(tuple(msgs), str(raw.get('get_updates_buf','')))
