"""Reconstructible, bounded reads; SQL metadata validates every cache hit."""
from collections import OrderedDict
from threading import RLock
from .core import digest

TABLES=('tasks','groups','pages','submissions','reference_drafts','reference_versions',
        'reference_metadata','scoring_drafts','scoring_versions','score_snapshots','runs',
        'work_items','attempts','cohorts','report_snapshots','intake_drafts','capture_sessions','capture_frames')


def stamp(service, task_id):
    with service.store.connection() as db:
        service.require_available(db,'tasks',task_id,allow_archived=True)
        values=[]
        for table in TABLES:
            where='id=?' if table=='tasks' else 'task_id=?'
            row=db.execute(f'SELECT COUNT(*),COALESCE(SUM(revision),0),MAX(updated_at) FROM {table} WHERE {where}',(task_id,)).fetchone()
            values.append(tuple(row))
        return digest([service.store.manifest['library_id'],values])


class ReadCache:
    def __init__(self, limit=64):
        self.values=OrderedDict();self.lock=RLock();self.limit=limit

    def get(self, key, token, build):
        with self.lock:
            old=self.values.get(key)
            if old and old[0]==token:
                self.values.move_to_end(key);return old[1]
            value=build()
            self.values[key]=(token,value);self.values.move_to_end(key)
            while len(self.values)>self.limit:self.values.popitem(last=False)
            return value


def detail(service, task_id, known_token=None):
    token=stamp(service,task_id)
    if known_token==token:return {'detail_unchanged':True,'cache_token':token}
    value=service.read_cache.get(('detail',task_id),token,lambda:service.detail(task_id))
    return {**value,'cache_token':token}


def statistics(service, task_id, group_id=None):
    from .reports import statistics as build
    token=stamp(service,task_id)
    value=service.read_cache.get(('statistics',task_id,group_id),token,
        lambda:build(service,task_id,[group_id] if group_id else None))
    return {**value,'cache_token':token}
