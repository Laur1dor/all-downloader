"""Safe public entry links and bounded reproduction metadata for owner alerts."""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit


def public_entry_url(value: str) -> str:
    """Rebuild a known post/track URL; never preserve credentials or CDN tokens."""
    if not isinstance(value,str) or len(value)>4096:
        return ''
    try:
        u=urlsplit(value)
        if u.scheme not in {'http','https'} or u.username or u.password or u.port not in (None,80 if u.scheme=='http' else 443):
            return ''
        host=(u.hostname or '').lower()
        path=unquote(u.path)
        q=parse_qs(u.query)
    except (ValueError,UnicodeError):
        return ''
    host=host.removeprefix('www.').removeprefix('m.')
    match=lambda pattern: re.fullmatch(pattern,path)
    result=''
    if host in {'vt.tiktok.com','vm.tiktok.com','t.tiktok.com'}:
        if match(r'/(?:t/)?[A-Za-z0-9_-]{4,64}/?'):result=f'https://{host}{path}'
    elif host=='tiktok.com':
        if match(r'/@[A-Za-z0-9_.-]{1,64}/(?:video|photo)/[0-9]{1,30}/?'):
            result='https://www.tiktok.com'+path.rstrip('/')
        elif match(r'/t/[A-Za-z0-9_-]{4,64}/?'):result='https://www.tiktok.com'+path
    elif host in {'youtube.com','music.youtube.com','youtu.be'}:
        from bot.youtube_posts import post_id
        post=post_id(value)
        if post:result='https://www.youtube.com/post/'+post
        else:
            ident=(path.strip('/') if host=='youtu.be' else q.get('v',[''])[0] if path.rstrip('/')=='/watch' else '')
            found=match(r'/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,32})/?')
            if found:ident=found[1]
            if re.fullmatch(r'[A-Za-z0-9_-]{6,32}',ident):
                result='https://www.youtube.com/watch?'+urlencode({'v':ident})
    elif host in {'instagram.com','instagr.am'}:
        if match(r'/(?:p|reel|reels|tv)/[A-Za-z0-9_-]{1,100}/?') or match(r'/stories/[A-Za-z0-9_.]{1,64}/[0-9]{1,30}/?'):
            result='https://www.instagram.com'+path.rstrip('/')+'/'
        elif match(r'/share/(?:reel|p)/[A-Za-z0-9_-]{1,100}/?'):
            result='https://www.instagram.com'+path.rstrip('/')+'/'
    elif host in {'x.com','twitter.com'}:
        found=match(r'/(?:[A-Za-z0-9_]{1,32}|i)/status/([0-9]{1,30})/?')
        if found:result='https://x.com/i/status/'+found[1]
    elif host=='rule34.xxx' and path=='/index.php':
        ident=q.get('id',[''])[0]
        if q.get('page')==['post'] and q.get('s')==['view'] and re.fullmatch(r'[0-9]{1,30}',ident):
            result='https://rule34.xxx/index.php?'+urlencode({'page':'post','s':'view','id':ident})
    elif host=='rule34video.com':
        found=match(r'/video/([0-9]{1,30})(?:/[A-Za-z0-9_-]{0,160})?/?')
        if found:result='https://rule34video.com/video/'+found[1]+'/'
    elif host in {'pornhub.com','pornhub.org'} or re.fullmatch(r'[a-z]{2,3}\.pornhub\.com',host):
        ident=q.get('viewkey',[''])[0]
        if path=='/view_video.php' and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',ident):
            result='https://www.pornhub.com/view_video.php?'+urlencode({'viewkey':ident})
        elif match(r'/shorties/[A-Za-z0-9_-]{1,64}/?'):result='https://www.pornhub.com'+path
    elif host=='pinterest.com' and match(r'/pin/[0-9]{1,30}/?'):
        result='https://www.pinterest.com'+path.rstrip('/')+'/'
    elif host=='pin.it' and match(r'/[A-Za-z0-9_-]{1,64}/?'):result='https://pin.it'+path
    elif host=='on.soundcloud.com' and match(r'/[A-Za-z0-9_-]{1,64}/?'):result='https://on.soundcloud.com'+path
    elif host=='soundcloud.com' and match(r'/[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_-]{1,100}/?'):
        if not path.endswith('/sets'):result='https://soundcloud.com'+path.rstrip('/')
    elif host in {'open.spotify.com','spotify.com'}:
        found=match(r'/(?:intl-[a-z]{2}/)?track/([A-Za-z0-9]{1,64})/?')
        if found:result='https://open.spotify.com/track/'+found[1]
    elif host in {'music.yandex.ru','music.yandex.com','music.yandex.by','music.yandex.kz'}:
        if match(r'/(?:album/[0-9]{1,30}/)?track/[0-9]{1,30}/?'):result=f'https://{host}'+path.rstrip('/')
    elif host=='the-joi-database.com':
        if path=='/' and re.fullmatch(r'[0-9]{1,30}',q.get('p',[''])[0]):
            result='https://the-joi-database.com/?'+urlencode({'p':q['p'][0]})
        elif (1<len(path)<=220 and not re.search(r'(?i)(?:wp-admin|wp-login|wp-json|oauth|/auth|/login|/token|\.(?:mp4|m3u8|m4a|webm))',path)):
            result='https://the-joi-database.com'+quote(path,safe='/-_.~')
    return result if len(result)<=512 else ''


def clean_context(value) -> dict:
    if not isinstance(value,dict):return {}
    result={}
    link=public_entry_url(value.get('url',''))
    resolved=public_entry_url(value.get('resolved_url',''))
    if not link:link=resolved
    if link:result['url']=link
    if resolved and resolved!=link:result['resolved_url']=resolved
    username=value.get('username')
    if isinstance(username,str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}',username):result['username']=username
    operation=value.get('operation')
    if isinstance(operation,str) and operation in {'video','audio','file','album','description','broadcast'}:result['operation']=operation
    span=value.get('time_range')
    if (isinstance(span,(list,tuple)) and len(span)==2 and all(type(x) is int for x in span)
            and 0<=span[0]<span[1]<=604800 and span[1]-span[0]<=14400):result['time_range']=list(span)
    height=value.get('height')
    if type(height) is int and 0<height<=9000:result['height']=height
    cause=value.get('cause_class')
    if isinstance(cause,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',cause):result['cause_class']=cause
    method=value.get('telegram_method')
    if isinstance(method,str) and method in {'sendVideo','sendDocument','sendAudio','sendPhoto','sendMediaGroup','sendLivePhoto','sendAnimation','sendVoice'}:
        result['telegram_method']=method
    return result


def user_link(user_id: int, username='') -> str:
    if type(user_id) is not int or user_id<=0:return 'служебная операция'
    name=clean_context({'username':username}).get('username')
    href='https://t.me/'+name if name else f'tg://user?id={user_id}'
    label=f'@{name} · ID {user_id}' if name else f'ID {user_id}'
    return f'<a href="{html.escape(href,quote=True)}">{html.escape(label)}</a>'


def reproduction(context) -> str:
    c=clean_context(context)
    operation={'video':'Видео','audio':'Музыка','file':'Файл','album':'Фото/альбом','description':'Описание','broadcast':'Рассылка'}.get(c.get('operation'),'Обработка запроса')
    text=f'Действие: {operation}.'
    if c.get('time_range'):
        text+=' Интервал: '+'–'.join(f'{v//3600:02}:{v//60%60:02}:{v%60:02}' for v in c['time_range'])+'.'
    if c.get('height'):text+=f" Выбранное качество: {c['height']}p."
    if c.get('url'):
        url=html.escape(c['url'],quote=True)
        text+=f'\nПубликация: <a href="{url}">{url}</a>\nДля проверки: отправить эту ссылку боту'
        if c.get('time_range'):text+=' с указанным интервалом'
        if c.get('operation') in {'file','audio','description'}:text+=f' и нажать «{operation}»'
        text+='.'
        if c.get('resolved_url'):
            resolved=html.escape(c['resolved_url'],quote=True)
            text+=f'\nПосле раскрытия: <a href="{resolved}">{resolved}</a>'
    else:text+='\nИсходная ссылка не сохранена прежней версией либо не является публичной ссылкой на публикацию.'
    if c.get('cause_class'):text+='\nПричина нижнего уровня: '+c['cause_class']+'.'
    if c.get('telegram_method'):text+=' Telegram: '+c['telegram_method']+'.'
    return text


def expected_input_error(error, *, kind='', url='', resolved_url='', stage='') -> bool:
    """Only definite user refusals; a broken CDN/transport is still a real fault."""
    if error is not None and type(error).__name__ in {'DeliveryUnknownError','TelegramForbiddenError','ChatBlocked'}:return False
    if error is not None and type(error).__name__ in {'BlockedAddressError','FileTooLargeError','OversizedError','AgeRestrictedError','DownloadCancelledError','TelegramRetryAfter'}:
        return True
    if kind in {'text','invalid_link','unsupported_link'} and stage not in {'upload','prepare'}:
        return True
    seen=set();pending=[error]
    while pending and len(seen)<8:
        current=pending.pop(0)
        if not isinstance(current,BaseException) or id(current) in seen:continue
        seen.add(id(current))
        if type(current).__name__=='TelegramBadRequest':
            method=getattr(getattr(current,'method',None),'__api_method__','')
            text=str(current)[:2000].lower()
            if method=='answerCallbackQuery' and any(p in text for p in ('query is too old','query id is invalid','response timeout expired')):return True
            if method in {'editMessageText','editMessageReplyMarkup'} and any(p in text for p in ('message is not modified','message to edit not found',"message can't be edited")):return True
        if stage not in {'prepare','upload'}:
            text=str(current)[:2000].lower()
            if any(p in text for p in ('this video has been removed','this video has been deleted',
                                      'video does not exist','this post has been removed','post does not exist','this video is private')):
                return True
            response=getattr(current,'response',None)
            code=getattr(current,'code',None) or getattr(response,'status_code',None) or getattr(response,'status',None)
            address=getattr(current,'url',None) or getattr(response,'url',None)
            entries={x for x in (url,resolved_url) if x}
            if code in {404,410} and address and public_entry_url(str(address)) in entries:return True
        pending.extend([current.__cause__,current.__context__])
        info=getattr(current,'exc_info',None)
        if isinstance(info,tuple) and len(info)==3:pending.append(info[1])
    return False
