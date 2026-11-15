"""Publisher compatibility and import failure backoff, without a ROS graph."""
from types import SimpleNamespace as NS

from collector.archive_ros import ArchiveRosAdapter, TopicSpec, subscription_policy


def profile(reliability, durability):
    return NS(qos_profile=NS(reliability=NS(name=reliability), durability=NS(name=durability)))


def test_radar_is_always_a_bounded_best_effort_subscriber():
    spec = TopicSpec('雷达MID360','/livox/lidar','CustomMsg')
    assert subscription_policy(spec,[profile('RELIABLE','TRANSIENT_LOCAL')]) == {
        'reliability':'BEST_EFFORT','durability':'VOLATILE','depth':8}


def test_map_history_is_one_and_static_transforms_are_bounded():
    assert subscription_policy(TopicSpec('定位与TF','/map','Map',True)) == {
        'reliability':'RELIABLE','durability':'TRANSIENT_LOCAL','depth':1}
    assert subscription_policy(TopicSpec('定位与TF','/tf_static','TF',True))['depth']==100


def test_volatile_or_best_effort_publishers_are_not_excluded_by_a_latched_label():
    spec=TopicSpec('定位与TF','/localization/state','String',True)
    assert subscription_policy(spec,[profile('BEST_EFFORT','VOLATILE')]) == {
        'reliability':'BEST_EFFORT','durability':'VOLATILE','depth':1}
    result=subscription_policy(spec,[profile('RELIABLE','TRANSIENT_LOCAL'),profile('BEST_EFFORT','VOLATILE')])
    assert result['reliability']=='BEST_EFFORT' and result['durability']=='VOLATILE'


class Node:
    def __init__(self):self.created=[];self.destroyed=[]
    def create_subscription(self,kind,topic,callback,qos,**options):
        value=NS(topic=topic,qos=qos,callback=callback);self.created.append(value);return value
    def destroy_subscription(self,value):self.destroyed.append(value);return True


class Store:
    def record(self,*args,**kwargs):return True


def test_missing_custom_message_retries_with_backoff_and_recovers():
    now=[0.0];calls=[];available=[False]
    adapter=ArchiveRosAdapter(Store(),lambda:True,sensors=['雷达MID360'],monotonic=lambda:now[0])
    def loader(name):
        calls.append(name)
        if not available[0]:raise ImportError('workspace not loaded')
        return name
    node=Node();adapter.bind(node,loader,lambda spec:NS(reliability='BEST_EFFORT',durability='VOLATILE',depth=8))
    try:
        adapter.reconcile_gate();assert len(calls)==2
        for _ in range(50):adapter.reconcile_gate()
        assert len(calls)==2
        now[0]=1;adapter.reconcile_gate();assert len(calls)==4
        available[0]=True;now[0]=3;adapter.reconcile_gate()
        assert len(node.created)==2 and not adapter.errors
        assert all(row['subscriptionRetryInS']==0 for row in adapter.snapshot()['topics'].values())
    finally:assert adapter.close()


def test_late_publisher_qos_is_repaired_without_touching_the_other_subscriptions():
    now=[0.0];durable=[True]
    adapter=ArchiveRosAdapter(Store(),lambda:True,sensors=['定位与TF'],monotonic=lambda:now[0])
    node=Node()
    def qos(spec):return NS(reliability='RELIABLE' if durable[0] else 'BEST_EFFORT',
        durability='TRANSIENT_LOCAL' if durable[0] else 'VOLATILE',depth=1)
    adapter.bind(node,lambda name:name,qos)
    try:
        adapter.reconcile_gate();count=len(node.created)
        continuous={topic:value for topic,value in adapter.subscriptions.items() if topic in ('/Odometry','/odom_nav','/tf')}
        durable[0]=False;now[0]=4;adapter.reconcile_gate();assert len(node.created)==count
        now[0]=5;adapter.reconcile_gate()
        assert node.destroyed and all(value.topic not in continuous for value in node.destroyed)
        assert all(adapter.subscriptions[topic] is value for topic,value in continuous.items())
        assert adapter.subscriptions['/map'].qos.durability=='VOLATILE'
    finally:assert adapter.close()


def test_qos_incompatibility_is_visible_and_not_falsely_counted_as_lost_messages():
    adapter=ArchiveRosAdapter(Store(),lambda:True,sensors=['定位与TF'])
    adapter.bind(Node(),lambda name:name,lambda spec:NS(reliability='BEST_EFFORT',durability='VOLATILE',depth=1))
    try:
        adapter.reconcile_gate()
        spec=next(item for item in adapter.specs if item.topic=='/map')
        adapter._message_lost(spec,NS(total_count_change=1,last_policy_kind='DURABILITY'))
        row=adapter.snapshot()['topics']['/map']
        assert row['state']=='ERROR' and row['incompatible_qos']==1 and row['dds_lost']==0
        adapter.receive(spec,b'untouched-cdr',adapter.epoch)
        assert adapter.snapshot()['topics']['/map']['error'] is None
    finally:assert adapter.close()
