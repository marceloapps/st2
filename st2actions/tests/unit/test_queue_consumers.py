# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
import st2tests.config as tests_config
tests_config.parse_args()

import mock
from kombu.message import Message

from st2actions import worker
from st2actions.scheduler import entrypoint as scheduling
from st2actions.scheduler import handler as scheduling_queue
from st2actions.container.base import RunnerContainer
from st2common.constants import action as action_constants
from st2common.models.db.liveaction import LiveActionDB
from st2common.models.system.common import ResourceReference
from st2common.persistence import action
from st2common.services import executions
from st2common.transport.publishers import PoolPublisher
from st2common.util import action_db
from st2common.util import date as date_utils
from st2tests.base import DbTestCase


@mock.patch.object(PoolPublisher, 'publish', mock.MagicMock())
@mock.patch.object(executions, 'update_execution', mock.MagicMock())
@mock.patch.object(Message, 'ack', mock.MagicMock())
class QueueConsumerTest(DbTestCase):

    def __init__(self, *args, **kwargs):
        super(QueueConsumerTest, self).__init__(*args, **kwargs)
        self.scheduler = scheduling.get_scheduler_entrypoint()
        self.scheduling_queue = scheduling_queue.get_handler()
        self.dispatcher = worker.get_worker()

    def _create_liveaction_db(self, status=action_constants.LIVEACTION_STATUS_REQUESTED):
        liveaction_db = LiveActionDB(
            action=ResourceReference(name='test_action', pack='test_pack').ref,
            parameters=None,
            start_timestamp=date_utils.get_datetime_utc_now(),
            status=status
        )

        return action.LiveAction.add_or_update(liveaction_db, publish=False)

    def _process_request(self, liveaction_db):
        self.scheduler._queue_consumer._process_message(liveaction_db)
        queued_request = self.scheduling_queue._get_next_execution()
        self.scheduling_queue._handle_execution(queued_request)

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(return_value={'key': 'value'}))
    def test_execute(self):
        liveaction_db = self._create_liveaction_db()
        self._process_request(liveaction_db)

        scheduled_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        scheduled_liveaction_db = self._wait_on_status(
            scheduled_liveaction_db,
            action_constants.LIVEACTION_STATUS_SCHEDULED
        )
        self.assertDictEqual(scheduled_liveaction_db.runner_info, {})

        self.dispatcher._queue_consumer._process_message(scheduled_liveaction_db)
        dispatched_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        self.assertGreater(len(list(dispatched_liveaction_db.runner_info.keys())), 0)
        self.assertEqual(
            dispatched_liveaction_db.status,
            action_constants.LIVEACTION_STATUS_RUNNING
        )

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(side_effect=Exception('Boom!')))
    def test_execute_failure(self):
        liveaction_db = self._create_liveaction_db()
        self._process_request(liveaction_db)

        scheduled_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        scheduled_liveaction_db = self._wait_on_status(
            scheduled_liveaction_db,
            action_constants.LIVEACTION_STATUS_SCHEDULED
        )

        self.dispatcher._queue_consumer._process_message(scheduled_liveaction_db)
        dispatched_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        self.assertEqual(dispatched_liveaction_db.status, action_constants.LIVEACTION_STATUS_FAILED)

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(return_value=None))
    def test_execute_no_result(self):
        liveaction_db = self._create_liveaction_db()
        self._process_request(liveaction_db)

        scheduled_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        scheduled_liveaction_db = self._wait_on_status(
            scheduled_liveaction_db,
            action_constants.LIVEACTION_STATUS_SCHEDULED
        )

        self.dispatcher._queue_consumer._process_message(scheduled_liveaction_db)
        dispatched_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        self.assertEqual(dispatched_liveaction_db.status, action_constants.LIVEACTION_STATUS_FAILED)

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(return_value=None))
    def test_execute_cancelation(self):
        liveaction_db = self._create_liveaction_db()
        self._process_request(liveaction_db)

        scheduled_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        scheduled_liveaction_db = self._wait_on_status(
            scheduled_liveaction_db,
            action_constants.LIVEACTION_STATUS_SCHEDULED
        )

        action_db.update_liveaction_status(
            status=action_constants.LIVEACTION_STATUS_CANCELED,
            liveaction_id=liveaction_db.id
        )

        canceled_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)
        self.dispatcher._queue_consumer._process_message(canceled_liveaction_db)
        dispatched_liveaction_db = action_db.get_liveaction_by_id(liveaction_db.id)

        self.assertEqual(
            dispatched_liveaction_db.status,
            action_constants.LIVEACTION_STATUS_CANCELED
        )

        self.assertDictEqual(
            dispatched_liveaction_db.result,
            {'message': 'Action execution canceled by user.'}
        )
