from lab import get_json

def first_page(base):
    payload=get_json(base+'/full')
    return {'门店':payload['value'],'后续链接':payload.get('nextLink')}

def inspect(base, fixture):
    return first_page(base)
