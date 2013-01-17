from django.contrib.auth.models import User
from django.utils.unittest.case import TestCase
from entity.models import Love, Entity
from feedly import get_redis_connection
from feedly.activity import Activity
from feedly.feed_managers.love_feedly import LoveFeedly
from feedly.feeds.love_feed import LoveFeed, DatabaseFallbackLoveFeed, \
    convert_activities_to_loves, LoveFeedItemCache
from feedly.marker import FeedEndMarker
from feedly.serializers.activity_serializer import ActivitySerializer
from feedly.serializers.love_activity_serializer import LoveActivitySerializer
from feedly.serializers.pickle_serializer import PickleSerializer
from feedly.structures.hash import RedisHashCache
from feedly.structures.list import RedisListCache
from feedly.utils import chunks
from feedly.verbs.base import Love as LoveVerb
from framework.utils.test import UserTestCase
from framework.utils.test.test_decorators import needs_love, needs_following, \
    needs_following_loves
from user.models_followers import Follow
import datetime
from feedly.aggregators.base import ModulusAggregator, RecentVerbAggregator,\
    NotificationAggregator
import random
from feedly.feeds.notification_feed import NotificationFeed
from feedly.feeds.aggregated_feed import AggregatedFeed
from feedly.feed_managers.notification_feedly import NotificationFeedly
from lists.models import ListItem
from feedly.serializers.aggregated_activity_serializer import AggregatedActivitySerializer
from pprint import pprint
import copy


class BaseFeedlyTestCase(UserTestCase):
    '''
    All other test cases should extend this one
    '''
    def assertActivityEqual(self, activity, comparison_activity, name=None):
        self.assertEqual(activity, comparison_activity)
            
    def assertAggregatedEqual(self, first, second):
        self.assertEqual(first, second)


class FeedlyTestCase(BaseFeedlyTestCase, UserTestCase):
    '''
    Test the feed manager

    The feed manager is responsible for the logic of handling follows, loves etc
    It mainly handles fanouts
    '''
    @needs_love
    @needs_following
    def test_add_love(self):
        feedly = LoveFeedly()
        love = Love.objects.filter(user=self.bogus_user)[:10][0]
        activity = feedly.create_love_activity(love)
        feeds = feedly.add_love(love)

        # make the love was added to all feeds
        for feed in feeds:
            love_added = feed.contains(activity)
            assert love_added, 'the love should be added'

    @needs_love
    @needs_following
    def test_remove_love(self):
        love = Love.objects.filter(user=self.bogus_user)[:10][0]
        feedly = LoveFeedly()
        feedly.add_love(love)
        feeds = feedly.remove_love(love)

        activity = feedly.create_love_activity(love)
        for feed in feeds:
            love_added = feed.contains(activity)
            assert not love_added, 'the love should be added'

    @needs_following_loves
    def test_follow(self):
        follow = Follow.objects.filter(user=self.bogus_user)[:1][0]
        # reset the feed
        feed = LoveFeed(follow.user_id)
        feed.delete()
        # do a follow
        feedly = LoveFeedly()
        feed = feedly.follow(follow)
        # see if we got the new loves
        target_loves = follow.target.get_profile().loves()[:10]
        for love in target_loves:
            activity = feedly.create_love_activity(love)
            assert feed.contains(activity)

        # check if we correctly broadcasted
        feedly.unfollow(follow)
        feed_count = feed.count()
        feed_results = feed[:20]
        self.assertEqual(feed_results, [])

    @needs_following_loves
    def test_follow_many(self):
        follows = Follow.objects.filter(user=self.bogus_user)[:5]
        follow = follows[0]
        # reset the feed
        feed = LoveFeed(follow.user_id)
        feed.delete()
        # do a follow
        feedly = LoveFeedly()
        feedly.follow_many(follows, async=False)
        # see if we got the new loves
        for follow in follows:
            target_loves = follow.target.get_profile().loves()[:10]
            for love in target_loves:
                activity = feedly.create_love_activity(love)
                assert feed.contains(activity)

        # check if we correctly broadcasted
        feedly.unfollow_many(follows)
        feed_count = feed.count()
        feed_results = feed[:20]
        self.assertEqual(feed_results, [])


class AggregateTestCase(BaseFeedlyTestCase, UserTestCase):
    @needs_following_loves
    def test_aggregate(self):
        profile = self.bogus_profile
        feed = profile.get_feed()
        loves = feed[:20]
        aggregator = ModulusAggregator(4)
        aggregated_activities = aggregator.aggregate(loves)
        self.assertLess(len(aggregated_activities), len(loves))
        
    @needs_following_loves
    def test_notifications(self):
        profile = self.bogus_profile
        feed = profile.get_feed()
        loves = feed[:20]
        # randomize the dates
        for l in loves:
            l.time = datetime.datetime(2012, 12, random.choice([1, 12, 14]))
        aggregator = RecentVerbAggregator()
        aggregated_activities = aggregator.aggregate(loves)
            

class AggregatedActivitySerializerTest(BaseFeedlyTestCase, UserTestCase):
    def test_basic_serialization(self):
        loves = Love.objects.all()[:10]
        activities = [l.create_activity() for l in loves]
        aggregator = NotificationAggregator()
        aggregated_activities = aggregator.aggregate(activities)
        serializer = AggregatedActivitySerializer()
        
        for aggregated in aggregated_activities:
            serialized = serializer.dumps(aggregated)
            unserialized = serializer.loads(serialized)
            self.assertAggregatedEqual(aggregated, unserialized)
            

class AggregatedFeedTestCase(BaseFeedlyTestCase, UserTestCase):
    def test_aggregated_feed(self):
        loves = Love.objects.all()[:10]
        feed = AggregatedFeed(13)
        # slow version
        activities = []
        feed.delete()
        for love in loves:
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            activities.append(activity)
            feed.add(activity)
            assert feed.contains(activity)
        
        #so we have something to compare to
        aggregator = RecentVerbAggregator()
        aggregated_activities = aggregator.aggregate(activities)
        # check the feed
        feed_loves = feed[:20]
        self.assertEqual(len(aggregated_activities), len(feed_loves))

        # now the fast version
        feed.delete()
        self.assertEqual(int(feed.count()), 0)
        feed.add_many(activities)
        for activity in activities:
            assert feed.contains(activity)
            
    def test_add_remove(self):
        '''
        Try to remove an aggregated activity
        '''
        loves = Love.objects.all()[:1]
        feed = AggregatedFeed(13)
        # slow version
        activities = []
        feed.delete()
        for love in loves:
            activity = love.create_activity()
            activities.append(activity)
            feed.add(activity)
            assert feed.contains(activity)
            aggregated_activity = feed[:10][0]
            feed.remove(aggregated_activity)
            assert not feed.contains(activity)
            
            
class NotificationFeedTestCase(BaseFeedlyTestCase, UserTestCase):
    def test_notification_feed(self):
        loves = Love.objects.all()[:10]
        feed = NotificationFeed(13)
        # slow version
        activities = []
        feed.delete()
        for love in loves:
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            activities.append(activity)
            feed.add(activity)
            assert feed.contains(activity)
        
        #so we have something to compare to
        aggregator = RecentVerbAggregator()
        aggregated_activities = aggregator.aggregate(activities)
        # check the feed
        feed_loves = feed[:20]
        self.assertEqual(len(aggregated_activities), len(feed_loves))

        # now the fast version
        feed.delete()
        self.assertEqual(int(feed.count()), 0)
        feed.add_many(activities)
        for activity in activities:
            assert feed.contains(activity)
            
        # test if we aggregated correctly
        self.assertEqual(feed.count_unseen(), len(aggregated_activities))
        # test marking as seen or read
        feed.mark_all(seen=True)
        # verify that the new count is 0
        self.assertEqual(feed.count_unseen(), 0)
        
    def test_add_remove(self):
        '''
        Try to remove an aggregated activity
        '''
        loves = Love.objects.all()[:1]
        feed = NotificationFeed(13)
        # slow version
        activities = []
        feed.delete()
        for love in loves:
            activity = love.create_activity()
            activities.append(activity)
            feed.add(activity)
            assert feed.contains(activity)
            aggregated_activity = feed[:10][0]
            feed.remove(aggregated_activity)
            assert not feed.contains(activity)
        
        
class NotificationFeedlyTestCase(BaseFeedlyTestCase, UserTestCase):
    def test_love(self):
        love = Love.objects.all()[:10][0]
        love.created_at = datetime.datetime.now()
        love.influencer_id = self.bogus_user.id
        influencer_feed = NotificationFeed(self.bogus_user.id)
        love.entity.created_by_id = self.bogus_user2.id
        creator_feed = NotificationFeed(self.bogus_user2.id)
        # we want to write two notifications
        # someone loved your find
        # someone loved your love
        notification_feedly = NotificationFeedly()
        # clean slate for testing
        influencer_feed.delete()
        creator_feed.delete()
        
        # comparison activity
        activity = love.create_activity()
        notification_feedly.add_love(love)
        
        # influencer feed
        assert influencer_feed.contains(activity)
        
        # creator feed
        creator_activity = copy.deepcopy(activity)
        creator_activity.extra_context['find'] = True
        assert creator_feed.contains(creator_activity)
        
    def test_serialization(self):
        '''
        Test if serialization doesnt take up too much memory
        '''
        notification_feed = NotificationFeed(self.bogus_user.id)
        notification_feed.delete()
        
        love = Love.objects.all()[:10][0]
        follow = Follow.objects.all()[:10][0]
        list_item = ListItem.objects.all()[:1][0]
        notification_feed.add(love.create_activity())
        notification_feed.add(follow.create_activity())
        notification_feed.add(list_item.create_activity())
        size = notification_feed.size()
        self.assertLess(size, 500)

    def test_scalability(self):
        '''
        Test if everything works if aggregating more than 50 activities
        in one aggregated activity
        '''
        notification_feed = NotificationFeed(self.bogus_user.id)
        notification_feed.delete()
        
        love = Love.objects.all()[:10][0]
        activities = []
        activity = love.create_activity()
        for x in range(110):
            activity = copy.deepcopy(activity)
            activity.extra_context['entity_id'] = x
            activities.append(activity)
            
        # add them all
        notification_feed.add_many(activities)
        
        # verify that our feed size doesn't escalate
        for aggregated in notification_feed[:notification_feed.max_length]:
            full_activities = len(aggregated.activities)
            activity_count = aggregated.activity_count
            self.assertEqual(full_activities, 100)
            self.assertEqual(activity_count, 110)
            actor_count = aggregated.actor_count
            self.assertLess(actor_count, 110)
            
        size = notification_feed.size()
        self.assertLess(size, 6000)
        
    def test_duplicates(self):
        '''
        The task system can often attempt to duplicate an insert
        This should raise an error to prevent weird data
        '''
        notification_feedly = NotificationFeedly()
        notification_feed = NotificationFeed(self.bogus_user.id)
        notification_feed.delete()
        
        love = Love.objects.all()[:10][0]
        
        for x in range(3):
            love.influencer_id = self.bogus_user.id
            notification_feedly.add_love(love)
            
        for aggregated in notification_feed[:notification_feed.max_length]:
            activity_count = aggregated.activity_count
            self.assertEqual(activity_count, 1)
        
    def test_follow(self):
        notification_feedly = NotificationFeedly()
        follows = Follow.objects.all()[:10]
        
        notification_feed = NotificationFeed(self.bogus_user.id)
        notification_feed.delete()
        
        for follow in follows:
            follow.user_id = self.bogus_user2.id
            follow.target_id = self.bogus_user.id
            follow.created_at = datetime.datetime.now()
            activity = follow.create_activity()
            feed = notification_feedly._follow(follow)
            assert feed.contains(activity)
        
        # influencer feed
        self.assertEqual(notification_feed.count_unseen(), 1)
        
    def test_add_to_list(self):
        notification_feedly = NotificationFeedly()
        notification_feed = NotificationFeed(self.bogus_user.id)
        list_items = ListItem.objects.all()[:1]
        
        for list_item in list_items:
            list_item.entity.created_by_id = self.bogus_user.id
            notification_feedly.add_to_list(list_item)
            activity = list_item.create_activity()
            
        assert notification_feed.contains(activity)
            
        
class SerializationTestCase(BaseFeedlyTestCase):
    def test_pickle_serializer(self):
        serializer = PickleSerializer()
        data = dict(hello='world')
        serialized = serializer.dumps(data)
        deserialized = serializer.loads(serialized)
        self.assertEqual(data, deserialized)

    def test_activity_serializer(self):
        serializer = ActivitySerializer()
        self._test_activity_serializer(serializer)

    def test_love_activity_serializer(self):
        love_serializer = LoveActivitySerializer()
        self._test_activity_serializer(love_serializer)

    def _test_activity_serializer(self, serializer):
        def test_activity(activity, name=None):
            serialized_activity = serializer.dumps(activity)
            deserialized = serializer.loads(serialized_activity)
            self.assertActivityEqual(activity, deserialized)

        # example with target
        activity = Activity(
            13, LoveVerb, 2000, target=15, time=datetime.datetime.now())
        test_activity(activity, 'target_no_context')
        # example with target and extra context
        activity = Activity(13, LoveVerb, 2000, target=15, time=datetime.datetime.now(), extra_context=dict(hello='world'))
        test_activity(activity, 'target_and_context')
        # example with no target and extra context
        activity = Activity(13, LoveVerb, 2000, time=datetime.datetime.now(
        ), extra_context=dict(hello='world'))
        test_activity(activity, 'no_target_and_context')
        # example with no target and no extra context
        activity = Activity(13, LoveVerb, 2000, time=datetime.datetime.now())
        test_activity(activity, 'no_target_and_no_context')


class RedisSortedSetTest(BaseFeedlyTestCase):

    def test_zremrangebyrank(self):
        redis = get_redis_connection()
        key = 'test'
        # start out fresh
        redis.delete(key)
        redis.zadd(key, 'a', 1)
        redis.zadd(key, 'b', 2)
        redis.zadd(key, 'c', 3)
        redis.zadd(key, 'd', 4)
        redis.zadd(key, 'e', 5)
        expected_results = [('a', 1.0), ('b', 2.0), ('c', 3.0), (
            'd', 4.0), ('e', 5.0)]
        results = redis.zrange(key, 0, -1, withscores=True)
        self.assertEqual(results, expected_results)
        results = redis.zrange(key, 0, -4, withscores=True)

        # now the idea is to only keep 3,4,5
        max_length = 3
        end = (max_length * -1) - 1
        redis.zremrangebyrank(key, 0, end)
        expected_results = [('c', 3.0), ('d', 4.0), ('e', 5.0)]
        results = redis.zrange(key, 0, -1, withscores=True)
        self.assertEqual(results, expected_results)


class LoveFeedTest(BaseFeedlyTestCase, UserTestCase):
    '''
    Test the basics of the feed
    - add love (add_many)
    - remove_love (remove_many)
    - read loves
    - follow user
    - unfollow user

    finished feeds don't do database queries
    unfinished feeds do database queries when the list is empty
    '''
    def test_count(self):
        loves = Love.objects.all()[:10]
        feed = LoveFeed(13)
        feed.finish()
        count_lazy = feed.count()
        count = int(count_lazy)

    def test_simple_add_love(self):
        loves = Love.objects.all()[:10]
        feed = LoveFeed(13)
        # slow version
        activities = []
        feed.delete()
        for love in loves:
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            activities.append(activity)
            feed.add(activity)
            assert feed.contains(activity)
        # close the feed
        feed.finish()
        feed_loves = feed[:20]
        assert isinstance(feed_loves[-1], FeedEndMarker)
        assert len(feed_loves) == 11
        for activity in feed_loves:
            assert activity
        # now the fast version
        feed.delete()
        feed.add_many(activities)
        for activity in activities:
            assert feed.contains(activity)

    def test_feed_trim(self):
        class SmallLoveFeed(LoveFeed):
            max_length = 5

        loves = Love.objects.all()[:10]
        feed = SmallLoveFeed(13)
        # slow version
        activities = []
        feed.delete()
        for love in loves:
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            activities.append(activity)
            feed.add(activity)
        # close the feed
        feed.finish()
        feed_loves = feed[:20]
        assert len(feed_loves) == feed.max_length
        for activity in feed_loves:
            assert activity

        # now the fast version
        feed.delete()
        feed.add_many(activities)

    def test_small_feed_instance(self):
        loves = Love.objects.all()[:5]
        feed = LoveFeed(13, max_length=2)
        for love in loves:
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            feed.add(activity)
        self.assertEqual(feed.count(), feed.max_length)

    def test_add_love(self):
        from entity.models import Love
        thessa = User.objects.get(pk=13)
        profile = thessa.get_profile()
        follower_ids = profile.cached_follower_ids()[:100]
        love = Love.objects.all()[:1][0]
        connection = get_redis_connection()

        # divide the followers in groups of 10000
        follower_groups = chunks(follower_ids, 10000)
        for follower_group in follower_groups:
            # now, for these 10000 items pipeline/thread away
            with connection.map() as redis:
                activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
                for follower_id in follower_group:
                    feed = LoveFeed(follower_id, redis=redis)
                    feed.add(activity)

    def test_follow(self):
        from user.models import Follow
        follow = Follow.objects.all()[:1][0]
        feed = LoveFeed(follow.user_id)
        target_loves = follow.target.get_profile().loves()[:500]
        for love in target_loves:
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            feed.add(activity)

        feed_loves = feed[:20]

    def test_simple_remove_love(self):
        from entity.models import Love
        target_loves = Love.objects.all()[:10]
        feed = LoveFeed(13)
        feed.delete()
        # slow implementation
        activities = []
        for love in target_loves:
            # remove the items by key (id)
            activity = Activity(love.user, LoveVerb, love, love.user, time=love.created_at, extra_context=dict(hello='world'))
            activities.append(activity)
            feed.remove(activity)

        feed.add_many(activities)
        for activity in activities:
            assert feed.contains(activity)
        feed.remove_many(activities)
        assert feed.count() == 0

        feed_loves = feed[:20]

    def test_remove_love(self):
        from entity.models import Love
        thessa = User.objects.get(pk=13)
        profile = thessa.get_profile()
        follower_ids = profile.cached_follower_ids()[:100]
        love = Love.objects.all()[:1][0]
        connection = get_redis_connection()

        # divide the followers in groups of 10000
        follower_groups = chunks(follower_ids, 10000)
        for follower_group in follower_groups:
            # now, for these 10000 items pipeline/thread away
            with connection.map() as redis:
                activity = love.create_activity()
                for follower_id in follower_group:
                    feed = LoveFeed(follower_id, redis=redis)
                    feed.remove(activity)


class DatabaseBackedLoveFeedTestCase(BaseFeedlyTestCase):
    def test_finish_marker_creation(self):
        # The user's feed is empty at the moment
        feed = DatabaseFallbackLoveFeed(self.bogus_user.id)
        feed.delete()
        results = feed[:100]
        self.assertEqual(results, [])
        self.assertEqual(feed.source, 'db')
        # now try reading the data only from redis
        results = feed[:100]
        self.assertEqual(feed.source, 'redis')
        # the finish marker should be there though
        self.assertEqual(feed.count(), 1)

    def test_double_finish(self):
        # validate that finish called twice acts as expected
        feed = DatabaseFallbackLoveFeed(self.bogus_user.id)
        feed.delete()
        feed.finish()
        feed.finish()
        self.assertEqual(feed.count(), 1)

    @needs_following_loves
    def test_empty_redis(self):
        # hack to make sure our queries work
        feed = DatabaseFallbackLoveFeed(self.bogus_user.id)
        feed.delete()

        # test the basic scenario if we have no data
        results = feed[:1]
        self.assertNotEqual(results, [])
        self.assertEqual(feed.source, 'db')
        results = feed[:1]
        self.assertEqual(feed.source, 'redis')

        # reset and test a finished empty list, this shouldnt return anything
        feed.delete()
        feed.finish()
        results = feed[:1]
        self.assertEqual(results, [])

        # try again past the first page
        feed.delete()
        results = feed[:1]
        results = feed[:2]
        self.assertEqual(len(results), 2)
        self.assertEqual(feed.source, 'db')

    @needs_following_loves
    def test_small_feed_instance(self):
        for desired_max_length in range(3, 5):
            feed = DatabaseFallbackLoveFeed(
                self.bogus_user.id, max_length=desired_max_length)
            feed.delete()

            # test the basic scenario if we have no data
            results = feed[:desired_max_length]
            results = feed[:desired_max_length]

            # this should come from redis, since its smaller than the desired max length
            self.assertEqual(feed.source, 'redis')
            self.assertEqual(len(results), desired_max_length)
            self.assertEqual(feed.max_length, desired_max_length)
            self.assertEqual(feed.count(), desired_max_length)

            # these will have to come from the db
            results = feed[:desired_max_length + 2]
            self.assertEqual(feed.source, 'db')
            results = feed[:desired_max_length + 2]
            self.assertEqual(feed.source, 'db')

    @needs_following_loves
    def test_enrichment(self):
        # hack to make sure our queries work
        feed = DatabaseFallbackLoveFeed(self.bogus_user.id)
        feed.delete()
        results = feed[:5]
        self.assertNotEqual(results, [])
        self.assertEqual(feed.source, 'db')
        results = feed[:5]
        self.assertEqual(feed.source, 'redis')

        # load the users and entities in batch
        # Transform to love objects
        for result in convert_activities_to_loves(results):
            assert isinstance(result, Love)
            assert isinstance(result.user, User)
            assert isinstance(result.entity, Entity)
            assert result.created_at, 'created_at is not defined'


class DatabaseBackedLoveFeedPaginationTestCase(BaseFeedlyTestCase):
    @needs_following_loves
    def test_filtering(self):
        # test the pagination
        feed = DatabaseFallbackLoveFeed(self.bogus_user.id)
        feed.delete()
        results = feed[:5]
        self.assertNotEqual(results, [])
        self.assertEqual(feed.source, 'db')
        one, two, three = feed[:3]
        assert one.object_id > two.object_id > three.object_id
        self.assertEqual(feed.source, 'redis')
        # we are sorted descending, this should get the first item
        feed.pk__gte = gte = two.object_id
        feed._set_filter()
        should_be_one = feed[:1][0]
        self.assertEqual(feed.source, 'redis')
        self.assertActivityEqual(should_be_one, one, name='should be one')
        # we are sorted descending, this should give the third item
        feed.pk__gte = None
        feed.pk__lte = lte = two.object_id - 1
        feed._set_filter()
        results = feed[:1]
        should_be_three = results[0]
        self.assertEqual(feed.source, 'redis')
        self.assertActivityEqual(
            should_be_three, three, name='should be three')


class BaseRedisStructureTestCase(BaseFeedlyTestCase):
    def get_structure(self):
        return


class ListCacheTestCase(BaseRedisStructureTestCase):
    def get_structure(self):
        structure = RedisListCache('test')
        structure.delete()
        return structure

    def test_append(self):
        cache = self.get_structure()
        cache.append_many(['a', 'b'])
        self.assertEqual(cache[:5], ['a', 'b'])
        self.assertEqual(cache.count(), 2)

    def test_remove(self):
        cache = self.get_structure()
        data = ['a', 'b']
        cache.append_many(data)
        self.assertEqual(cache[:5], data)
        self.assertEqual(cache.count(), 2)
        for value in data:
            cache.remove(value)
        self.assertEqual(cache[:5], [])
        self.assertEqual(cache.count(), 0)


class HashCacheTestCase(BaseRedisStructureTestCase):
    def get_structure(self):
        structure = RedisHashCache('test')
        # always start fresh
        structure.delete()
        return structure

    def test_set_many(self):
        cache = self.get_structure()
        key_value_pairs = [('key', 'value'), ('key2', 'value2')]
        cache.set_many(key_value_pairs)

    def test_get_and_set(self):
        cache = self.get_structure()
        key_value_pairs = [('key', 'value'), ('key2', 'value2')]
        cache.set_many(key_value_pairs)
        results = cache.get_many(['key', 'key2'])
        self.assertEqual(results, {'key2': 'value2', 'key': 'value'})

        result = cache.get('key')
        self.assertEqual(result, 'value')

        result = cache.get('key_missing')
        self.assertEqual(result, None)

    def test_contains(self):
        cache = self.get_structure()
        key_value_pairs = [('key', 'value'), ('key2', 'value2')]
        cache.set_many(key_value_pairs)
        result = cache.contains('key')
        self.assertEqual(result, True)
        result = cache.contains('key2')
        self.assertEqual(result, True)
        result = cache.contains('key_missing')
        self.assertEqual(result, False)

    def test_count(self):
        cache = self.get_structure()
        key_value_pairs = [('key', 'value'), ('key2', 'value2')]
        cache.set_many(key_value_pairs)
        count = cache.count()
        self.assertEqual(count, 2)


class LoveFeedItemCacheTestCase(BaseRedisStructureTestCase):
    def get_structure(self):
        structure = LoveFeedItemCache('global')
        # always start fresh
        structure.delete()
        return structure

    @needs_following_loves
    def test_db_fallback(self):
        cache = self.get_structure()
        # hack to make sure our queries work
        feed = DatabaseFallbackLoveFeed(self.bogus_user.id)
        feed.delete()

        # test the basic scenario if we have no data
        results = feed[:10]
        self.assertNotEqual(results, [])
        self.assertEqual(feed.source, 'db')
        cache_count = cache.count()
        self.assertNotEqual(cache_count, 0)

        # now to test the db fallback
        keys = cache.keys()
        to_remove = keys[:3]
        redis_results = cache.get_many(to_remove)
        cache.delete_many(to_remove)
        self.assertNotEqual(cache.count(), cache_count)

        # now proceed to lookup the missing keys
        db_results = cache.get_many(to_remove)
        # these should still load from the db
        self.assertEqual(redis_results, db_results)

        db_results = cache.get_many(to_remove)