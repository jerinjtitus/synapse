# -*- coding: utf-8 -*-
# Copyright 2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from mock import Mock

from synapse.api.constants import EduTypes
from synapse.events import EventBase
from synapse.federation.units import Transaction
from synapse.handlers.presence import UserPresenceState
from synapse.rest import admin
from synapse.rest.client.v1 import login, presence, room
from synapse.types import create_requester

from tests.events.test_presence_router import send_presence_update, sync_presence
from tests.test_utils.event_injection import inject_member_event
from tests.unittest import FederatingHomeserverTestCase, override_config


class ModuleApiTestCase(FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        presence.register_servlets,
    ]

    def prepare(self, reactor, clock, homeserver):
        self.store = homeserver.get_datastore()
        self.module_api = homeserver.get_module_api()
        self.event_creation_handler = homeserver.get_event_creation_handler()
        self.sync_handler = homeserver.get_sync_handler()

    def make_homeserver(self, reactor, clock):
        return self.setup_test_homeserver(
            federation_transport_client=Mock(spec=["send_transaction"]),
        )

    def test_can_register_user(self):
        """Tests that an external module can register a user"""
        # Register a new user
        user_id, access_token = self.get_success(
            self.module_api.register(
                "bob", displayname="Bobberino", emails=["bob@bobinator.bob"]
            )
        )

        # Check that the new user exists with all provided attributes
        self.assertEqual(user_id, "@bob:test")
        self.assertTrue(access_token)
        self.assertTrue(self.get_success(self.store.get_user_by_id(user_id)))

        # Check that the email was assigned
        emails = self.get_success(self.store.user_get_threepids(user_id))
        self.assertEqual(len(emails), 1)

        email = emails[0]
        self.assertEqual(email["medium"], "email")
        self.assertEqual(email["address"], "bob@bobinator.bob")

        # Should these be 0?
        self.assertEqual(email["validated_at"], 0)
        self.assertEqual(email["added_at"], 0)

        # Check that the displayname was assigned
        displayname = self.get_success(self.store.get_profile_displayname("bob"))
        self.assertEqual(displayname, "Bobberino")

    def test_sending_events_into_room(self):
        """Tests that a module can send events into a room"""
        # Mock out create_and_send_nonmember_event to check whether events are being sent
        self.event_creation_handler.create_and_send_nonmember_event = Mock(
            spec=[],
            side_effect=self.event_creation_handler.create_and_send_nonmember_event,
        )

        # Create a user and room to play with
        user_id = self.register_user("summer", "monkey")
        tok = self.login("summer", "monkey")
        room_id = self.helper.create_room_as(user_id, tok=tok)

        # Create and send a non-state event
        content = {"body": "I am a puppet", "msgtype": "m.text"}
        event_dict = {
            "room_id": room_id,
            "type": "m.room.message",
            "content": content,
            "sender": user_id,
        }
        event = self.get_success(
            self.module_api.create_and_send_event_into_room(event_dict)
        )  # type: EventBase
        self.assertEqual(event.sender, user_id)
        self.assertEqual(event.type, "m.room.message")
        self.assertEqual(event.room_id, room_id)
        self.assertFalse(hasattr(event, "state_key"))
        self.assertDictEqual(event.content, content)

        expected_requester = create_requester(
            user_id, authenticated_entity=self.hs.hostname
        )

        # Check that the event was sent
        self.event_creation_handler.create_and_send_nonmember_event.assert_called_with(
            expected_requester,
            event_dict,
            ratelimit=False,
            ignore_shadow_ban=True,
        )

        # Create and send a state event
        content = {
            "events_default": 0,
            "users": {user_id: 100},
            "state_default": 50,
            "users_default": 0,
            "events": {"test.event.type": 25},
        }
        event_dict = {
            "room_id": room_id,
            "type": "m.room.power_levels",
            "content": content,
            "sender": user_id,
            "state_key": "",
        }
        event = self.get_success(
            self.module_api.create_and_send_event_into_room(event_dict)
        )  # type: EventBase
        self.assertEqual(event.sender, user_id)
        self.assertEqual(event.type, "m.room.power_levels")
        self.assertEqual(event.room_id, room_id)
        self.assertEqual(event.state_key, "")
        self.assertDictEqual(event.content, content)

        # Check that the event was sent
        self.event_creation_handler.create_and_send_nonmember_event.assert_called_with(
            expected_requester,
            {
                "type": "m.room.power_levels",
                "content": content,
                "room_id": room_id,
                "sender": user_id,
                "state_key": "",
            },
            ratelimit=False,
            ignore_shadow_ban=True,
        )

        # Check that we can't send membership events
        content = {
            "membership": "leave",
        }
        event_dict = {
            "room_id": room_id,
            "type": "m.room.member",
            "content": content,
            "sender": user_id,
            "state_key": user_id,
        }
        self.get_failure(
            self.module_api.create_and_send_event_into_room(event_dict), Exception
        )

    def test_public_rooms(self):
        """Tests that a room can be added and removed from the public rooms list,
        as well as have its public rooms directory state queried.
        """
        # Create a user and room to play with
        user_id = self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")
        room_id = self.helper.create_room_as(user_id, tok=tok)

        # The room should not currently be in the public rooms directory
        is_in_public_rooms = self.get_success(
            self.module_api.public_room_list_manager.room_is_in_public_room_list(
                room_id
            )
        )
        self.assertFalse(is_in_public_rooms)

        # Let's try adding it to the public rooms directory
        self.get_success(
            self.module_api.public_room_list_manager.add_room_to_public_room_list(
                room_id
            )
        )

        # And checking whether it's in there...
        is_in_public_rooms = self.get_success(
            self.module_api.public_room_list_manager.room_is_in_public_room_list(
                room_id
            )
        )
        self.assertTrue(is_in_public_rooms)

        # Let's remove it again
        self.get_success(
            self.module_api.public_room_list_manager.remove_room_from_public_room_list(
                room_id
            )
        )

        # Should be gone
        is_in_public_rooms = self.get_success(
            self.module_api.public_room_list_manager.room_is_in_public_room_list(
                room_id
            )
        )
        self.assertFalse(is_in_public_rooms)

    # The ability to send federation is required by send_local_online_presence_to.
    @override_config({"send_federation": True})
    def test_send_local_online_presence_to(self):
        """Tests that send_local_presence_to_users sends local online presence to local users."""
        # Create a user who will send presence updates
        self.presence_receiver_id = self.register_user("presence_receiver", "monkey")
        self.presence_receiver_tok = self.login("presence_receiver", "monkey")

        # And another user that will send presence updates out
        self.presence_sender_id = self.register_user("presence_sender", "monkey")
        self.presence_sender_tok = self.login("presence_sender", "monkey")

        # Put them in a room together so they will receive each other's presence updates
        room_id = self.helper.create_room_as(
            self.presence_receiver_id,
            tok=self.presence_receiver_tok,
        )
        self.helper.join(room_id, self.presence_sender_id, tok=self.presence_sender_tok)

        # Presence sender comes online
        send_presence_update(
            self,
            self.presence_sender_id,
            self.presence_sender_tok,
            "online",
            "I'm online!",
        )

        # Presence receiver should have received it
        presence_updates, sync_token = sync_presence(self, self.presence_receiver_id)
        self.assertEqual(len(presence_updates), 1)

        presence_update = presence_updates[0]  # type: UserPresenceState
        self.assertEqual(presence_update.user_id, self.presence_sender_id)
        self.assertEqual(presence_update.state, "online")

        # Syncing again should result in no presence updates
        presence_updates, sync_token = sync_presence(
            self, self.presence_receiver_id, sync_token
        )
        self.assertEqual(len(presence_updates), 0)

        # Trigger sending local online presence
        self.get_success(
            self.module_api.send_local_online_presence_to(
                [
                    self.presence_receiver_id,
                ]
            )
        )

        # Presence receiver should have received online presence again
        presence_updates, sync_token = sync_presence(
            self, self.presence_receiver_id, sync_token
        )
        self.assertEqual(len(presence_updates), 1)

        presence_update = presence_updates[0]  # type: UserPresenceState
        self.assertEqual(presence_update.user_id, self.presence_sender_id)
        self.assertEqual(presence_update.state, "online")

        # Presence sender goes offline
        send_presence_update(
            self,
            self.presence_sender_id,
            self.presence_sender_tok,
            "offline",
            "I slink back into the darkness.",
        )

        # Trigger sending local online presence
        self.get_success(
            self.module_api.send_local_online_presence_to(
                [
                    self.presence_receiver_id,
                ]
            )
        )

        # Presence receiver should *not* have received offline state
        presence_updates, sync_token = sync_presence(
            self, self.presence_receiver_id, sync_token
        )
        self.assertEqual(len(presence_updates), 0)

    @override_config({"send_federation": True})
    def test_send_local_online_presence_to_federation(self):
        """Tests that send_local_presence_to_users sends local online presence to remote users."""
        # Create a user who will send presence updates
        self.presence_sender_id = self.register_user("presence_sender", "monkey")
        self.presence_sender_tok = self.login("presence_sender", "monkey")

        # And a room they're a part of
        room_id = self.helper.create_room_as(
            self.presence_sender_id,
            tok=self.presence_sender_tok,
        )

        # Mark them as online
        send_presence_update(
            self,
            self.presence_sender_id,
            self.presence_sender_tok,
            "online",
            "I'm online!",
        )

        # Make up a remote user to send presence to
        remote_user_id = "@far_away_person:island"

        # Create a join membership event for the remote user into the room.
        # This allows presence information to flow from one user to the other.
        self.get_success(
            inject_member_event(
                self.hs,
                room_id,
                sender=remote_user_id,
                target=remote_user_id,
                membership="join",
            )
        )

        # The remote user would have received the existing room members' presence
        # when they joined the room.
        #
        # Thus we reset the mock, and try sending online local user
        # presence again
        self.hs.get_federation_transport_client().send_transaction.reset_mock()

        # Broadcast local user online presence
        self.get_success(
            self.module_api.send_local_online_presence_to([remote_user_id])
        )

        # Check that a presence update was sent as part of a federation transaction
        found_update = False
        calls = (
            self.hs.get_federation_transport_client().send_transaction.call_args_list
        )
        for call in calls:
            federation_transaction = call.args[0]  # type: Transaction

            # Get the sent EDUs in this transaction
            edus = federation_transaction.get_dict()["edus"]

            for edu in edus:
                # Make sure we're only checking presence-type EDUs
                if edu["edu_type"] != EduTypes.Presence:
                    continue

                # EDUs can contain multiple presence updates
                for presence_update in edu["content"]["push"]:
                    if presence_update["user_id"] == self.presence_sender_id:
                        found_update = True

        self.assertTrue(found_update)
