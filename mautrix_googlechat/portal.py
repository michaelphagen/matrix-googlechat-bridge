# mautrix-googlechat - A Matrix-Google Chat puppeting bridge
# Copyright (C) 2021 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import (Dict, Deque, Optional, Tuple, Union, Set, List, Any, AsyncIterable, NamedTuple,
                    cast, TYPE_CHECKING)
from collections import deque
import mimetypes
import asyncio
import random
import time
import io

import magic
from yarl import URL
import aiohttp

from maugclib import googlechat_pb2 as googlechat, FileTooLargeError

from mautrix.types import (RoomID, MessageEventContent, EventID, MessageType, EventType, ImageInfo,
                           TextMessageEventContent, MediaMessageEventContent, Membership, UserID,
                           PowerLevelStateEventContent, ContentURI, EncryptionAlgorithm)
from mautrix.appservice import IntentAPI
from mautrix.bridge import BasePortal, NotificationDisabler, async_getter_lock
from mautrix.util.message_send_checkpoint import MessageSendCheckpointStatus
from mautrix.errors import MatrixError, MForbidden

from .config import Config
from .db import Portal as DBPortal, Message as DBMessage, Reaction as DBReaction
from . import puppet as p, user as u, formatter as fmt

if TYPE_CHECKING:
    from .__main__ import GoogleChatBridge
    from .matrix import MatrixHandler

try:
    from mautrix.crypto.attachments import decrypt_attachment, encrypt_attachment
except ImportError:
    decrypt_attachment = encrypt_attachment = None


class FakeLock:
    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass


StateBridge = EventType.find("m.bridge", EventType.Class.STATE)
StateHalfShotBridge = EventType.find("uk.half-shot.bridge", EventType.Class.STATE)

SendResponse = NamedTuple('SendResponse', gcid=str, timestamp=int)
ChatInfo = Union[googlechat.WorldItemLite, googlechat.GetGroupResponse]


class Portal(DBPortal, BasePortal):
    invite_own_puppet_to_pm: bool = False
    by_mxid: Dict[RoomID, 'Portal'] = {}
    by_gcid: Dict[Tuple[str, str], 'Portal'] = {}
    matrix: 'MatrixHandler'
    config: Config

    _main_intent: Optional[IntentAPI]
    _create_room_lock: asyncio.Lock
    _last_bridged_mxid: Optional[EventID]
    _dedup: Deque[str]
    _local_dedup: Set[str]
    _send_locks: Dict[str, asyncio.Lock]
    _edit_dedup: Dict[str, int]
    _noop_lock: FakeLock = FakeLock()
    _typing: Set[UserID]
    _backfill_lock: asyncio.Lock

    def __init__(self, gcid: str, gc_receiver: str, other_user_id: Optional[str] = None,
                 mxid: Optional[RoomID] = None, name: Optional[str] = None,
                 avatar_mxc: Optional[ContentURI] = None, name_set: bool = False,
                 avatar_set: bool = False, encrypted: bool = False, revision: Optional[int] = None,
                 is_threaded: Optional[bool] = None) -> None:
        super().__init__(gcid=gcid, gc_receiver=gc_receiver, other_user_id=other_user_id,
                         mxid=mxid, name=name, avatar_mxc=avatar_mxc, name_set=name_set,
                         avatar_set=avatar_set, encrypted=encrypted, revision=revision,
                         is_threaded=is_threaded)
        self.log = self.log.getChild(self.gcid_log)

        self._main_intent = None
        self._create_room_lock = asyncio.Lock()
        self._backfill_lock = asyncio.Lock()
        self._last_bridged_mxid = None
        self._dedup = deque(maxlen=100)
        self._edit_dedup = {}
        self._local_dedup = set()
        self._send_locks = {}
        self._typing = set()

    @classmethod
    def init_cls(cls, bridge: 'GoogleChatBridge') -> None:
        BasePortal.bridge = bridge
        cls.az = bridge.az
        cls.config = bridge.config
        cls.loop = bridge.loop
        cls.matrix = bridge.matrix
        cls.invite_own_puppet_to_pm = cls.config["bridge.invite_own_puppet_to_pm"]
        NotificationDisabler.puppet_cls = p.Puppet
        NotificationDisabler.config_enabled = cls.config["bridge.backfill.disable_notifications"]

    @property
    def gcid_full(self) -> Tuple[str, str]:
        return self.gcid, self.gc_receiver

    @property
    def gcid_plain(self) -> str:
        gc_type, gcid = self.gcid.split(":")
        return gcid

    @property
    def gcid_log(self) -> str:
        if self.is_direct:
            return f"{self.gcid}-{self.gc_receiver}"
        return self.gcid

    # region DB conversion

    async def delete(self) -> None:
        if self.mxid:
            await DBMessage.delete_all_by_room(self.mxid)
            await DBReaction.delete_all_by_room(self.mxid)
        self.by_gcid.pop(self.gcid_full, None)
        self.by_mxid.pop(self.mxid, None)
        await super().delete()

    # endregion
    # region Properties

    @property
    def is_direct(self) -> bool:
        return self.is_dm and bool(self.other_user_id)

    @property
    def is_dm(self) -> bool:
        return self.gcid.startswith("dm:")

    @property
    def main_intent(self) -> IntentAPI:
        if not self._main_intent:
            raise ValueError("Portal must be postinit()ed before main_intent can be used")
        return self._main_intent

    # endregion
    # region Chat info updating

    async def update_info(self, source: Optional['u.User'] = None, info: Optional[ChatInfo] = None
                          ) -> ChatInfo:
        if not info or (not self.is_dm and isinstance(info, googlechat.WorldItemLite)):
            info = await source.get_group(self.gcid)

        changed = False
        is_threaded = (info if isinstance(info, googlechat.WorldItemLite)
                       else info.group).HasField("threaded_group")
        if is_threaded != self.is_threaded:
            self.is_threaded = is_threaded
            changed = True
        changed = await self._update_participants(source, info) or changed
        changed = await self._update_name(info) or changed
        if changed:
            await self.save()
            await self.update_bridge_info()
        return info

    async def _update_name(self, info: ChatInfo) -> bool:
        if self.is_direct:
            puppet = await p.Puppet.get_by_gcid(self.other_user_id)
            name = puppet.name
        elif isinstance(info, googlechat.WorldItemLite) and info.HasField("room_name"):
            name = info.room_name
        elif isinstance(info, googlechat.GetGroupResponse) and info.group.HasField("name"):
            name = info.group.name
        else:
            return False
        if self.name != name:
            self.name = name
            if self.mxid and (self.encrypted or not self.is_direct):
                await self.main_intent.set_room_name(self.mxid, self.name)
            return True
        return False

    def _get_invite_content(self, double_puppet: Optional['p.Puppet']) -> Dict[str, Any]:
        invite_content = {}
        if double_puppet:
            invite_content["fi.mau.will_auto_accept"] = True
        if self.is_direct:
            invite_content["is_direct"] = True
        return invite_content

    async def _update_participants(self, source: 'u.User', info: ChatInfo) -> None:
        if (
            self.is_dm and isinstance(info, googlechat.WorldItemLite)
            and info.HasField("dm_members")
        ):
            user_ids = [member.id for member in info.dm_members.members]
        elif isinstance(info, googlechat.GetGroupResponse):
            user_ids = [member.id.member_id.user_id.id for member in info.memberships]
        else:
            raise ValueError("No participants found :(")
        if self.is_dm and len(user_ids) == 2:
            user_ids.remove(source.gcid)
        if self.is_dm and len(user_ids) == 1 and not self.other_user_id:
            self.other_user_id = user_ids[0]
            self._main_intent = (await p.Puppet.get_by_gcid(self.other_user_id)
                                 ).default_mxid_intent
            await self.save()
        if not self.mxid and not self.is_direct:
            return
        users = await source.get_users(user_ids)
        await asyncio.gather(*[self._update_participant(source, user) for user in users])

    async def _update_participant(self, source: 'u.User', user: googlechat.User) -> None:
        puppet = await p.Puppet.get_by_gcid(user.user_id.id)
        await puppet.update_info(source=source, info=user)
        if self.mxid:
            await puppet.intent_for(self).ensure_joined(self.mxid)

    # endregion

    async def backfill(self, source: 'u.User', is_initial: bool = False) -> None:
        try:
            async with self._backfill_lock:
                await self._backfill(source, is_initial=is_initial)
        except Exception:
            self.log.exception(f"Fatal error while backfilling ({is_initial=})")

    async def _backfill(self, source: 'u.User', is_initial: bool = False) -> None:
        if not is_initial and not self.revision:
            # self.log.warning("Can't do catch-up backfill on portal with no known last revision")
            return


    # async def _load_messages(self, source: 'u.User', limit: int = 100,
    #                          token: Any = None
    #                          ) -> Tuple[List[ChatMessageEvent], Any]:
    #     resp = await source.client.get_conversation(hangouts.GetConversationRequest(
    #         request_header=source.client.get_request_header(),
    #         conversation_spec=hangouts.ConversationSpec(
    #             conversation_id=hangouts.ConversationId(id=self.gid),
    #         ),
    #         include_conversation_metadata=False,
    #         include_event=True,
    #         max_events_per_conversation=limit,
    #         event_continuation_token=token
    #     ))
    #     return ([HangoutsChat._wrap_event(evt) for evt in resp.conversation_state.event],
    #             resp.conversation_state.event_continuation_token)
    #     return [], None
    #
    # async def _load_many_messages(self, source: 'u.User', is_initial: bool
    #                               ) -> List[ChatMessageEvent]:
    #     limit = (self.config["bridge.backfill.initial_limit"] if is_initial
    #              else self.config["bridge.backfill.missed_limit"])
    #     if limit <= 0:
    #         return []
    #     messages = []
    #     self.log.debug("Fetching up to %d messages through %s", limit, source.gcid)
    #     token = None
    #     while limit > 0:
    #         chunk_limit = min(limit, 100)
    #         chunk, token = await self._load_messages(source, chunk_limit, token)
    #         for message in reversed(chunk):
    #             if await DBMessage.get_by_gcid(message.msg_id, self.gc_receiver):
    #                 self.log.debug("Stopping backfilling at %s (ts: %s) "
    #                                "as message was already bridged",
    #                                message.msg_id, message.timestamp)
    #                 break
    #             messages.append(message)
    #         if len(chunk) < chunk_limit:
    #             break
    #         limit -= len(chunk)
    #     return messages
    #
    # async def backfill(self, source: 'u.User', is_initial: bool = False) -> None:
    #     if not TYPE_CHECKING:
    #         self.log.debug("Backfill is not yet implemented")
    #         return
    #     try:
    #         with self.backfill_lock:
    #             await self._backfill(source, is_initial)
    #     except Exception:
    #         self.log.exception("Failed to backfill portal")
    #
    # async def _backfill(self, source: 'u.User', is_initial: bool = False) -> None:
    #     self.log.debug("Backfilling history through %s", source.mxid)
    #     messages = await self._load_many_messages(source, is_initial)
    #     if not messages:
    #         self.log.debug("Didn't get any messages from server")
    #         return
    #     self.log.debug("Got %d messages from server", len(messages))
    #     backfill_leave = set()
    #     if self.config["bridge.backfill.invite_own_puppet"]:
    #         self.log.debug("Adding %s's default puppet to room for backfilling", source.mxid)
    #         sender = await p.Puppet.get_by_gcid(source.gcid)
    #         await self.main_intent.invite_user(self.mxid, sender.default_mxid)
    #         await sender.default_mxid_intent.join_room_by_id(self.mxid)
    #         backfill_leave.add(sender.default_mxid_intent)
    #     async with NotificationDisabler(self.mxid, source):
    #         for message in reversed(messages):
    #             if isinstance(message, ChatMessageEvent):
    #                 puppet = await p.Puppet.get_by_gcid(message.user_id)
    #                 await self.handle_googlechat_message(source, puppet, message)
    #             else:
    #                 self.log.trace("Unhandled event type %s while backfilling", type(message))
    #     for intent in backfill_leave:
    #         self.log.trace("Leaving room with %s post-backfill", intent.mxid)
    #         await intent.leave_room(self.mxid)
    #     self.log.info("Backfilled %d messages through %s", len(messages), source.mxid)

    # region Matrix room creation

    async def _update_matrix_room(self, source: 'u.User', info: Optional[ChatInfo] = None) -> None:
        puppet = await p.Puppet.get_by_custom_mxid(source.mxid)
        await self.main_intent.invite_user(self.mxid, source.mxid,
                                           extra_content=self._get_invite_content(puppet))
        if puppet:
            did_join = await puppet.intent.ensure_joined(self.mxid)
            if did_join and self.is_direct:
                await source.update_direct_chats({self.main_intent.mxid: [self.mxid]})

        await self.main_intent.invite_user(self.mxid, source.mxid, check_cache=True)
        puppet = await p.Puppet.get_by_custom_mxid(source.mxid)
        if puppet:
            await puppet.intent.ensure_joined(self.mxid)
        await self.update_info(source, info)

    async def update_matrix_room(self, source: 'u.User', info: Optional[ChatInfo] = None) -> None:
        try:
            await self._update_matrix_room(source, info)
        except Exception:
            self.log.exception("Failed to update portal")

    async def create_matrix_room(self, source: 'u.User', info: Optional[ChatInfo] = None
                                 ) -> RoomID:
        if self.mxid:
            await self.update_matrix_room(source, info)
            return self.mxid
        async with self._create_room_lock:
            try:
                return await self._create_matrix_room(source, info)
            except Exception:
                self.log.exception("Failed to create portal")

    @property
    def bridge_info_state_key(self) -> str:
        return f"net.maunium.googlechat://googlechat/{self.gcid}"

    @property
    def bridge_info(self) -> Dict[str, Any]:
        return {
            "bridgebot": self.az.bot_mxid,
            "creator": self.main_intent.mxid,
            "protocol": {
                "id": "googlechat",
                "displayname": "Google Chat",
                "avatar_url": self.config["appservice.bot_avatar"],
            },
            "channel": {
                "id": self.gcid,
                "displayname": self.name,
                "fi.mau.googlechat.is_threaded": self.is_threaded,
            }
        }

    async def update_bridge_info(self) -> None:
        if not self.mxid:
            self.log.debug("Not updating bridge info: no Matrix room created")
            return
        try:
            self.log.debug("Updating bridge info...")
            await self.main_intent.send_state_event(self.mxid, StateBridge,
                                                    self.bridge_info, self.bridge_info_state_key)
            # TODO remove this once https://github.com/matrix-org/matrix-doc/pull/2346 is in spec
            await self.main_intent.send_state_event(self.mxid, StateHalfShotBridge,
                                                    self.bridge_info, self.bridge_info_state_key)
        except Exception:
            self.log.warning("Failed to update bridge info", exc_info=True)

    async def _create_matrix_room(self, source: 'u.User', info: Optional[ChatInfo] = None
                                  ) -> RoomID:
        if self.mxid:
            await self._update_matrix_room(source, info)
            return self.mxid

        info = await self.update_info(source=source, info=info)
        self.log.debug("Creating Matrix room")
        power_levels = PowerLevelStateEventContent()
        invites = []
        if self.is_direct:
            power_levels.users[source.mxid] = 50
        power_levels.users[self.main_intent.mxid] = 100
        initial_state = [{
            "type": str(EventType.ROOM_POWER_LEVELS),
            "content": power_levels.serialize(),
        }, {
            "type": str(StateBridge),
            "state_key": self.bridge_info_state_key,
            "content": self.bridge_info,
        }, {
            # TODO remove this once https://github.com/matrix-org/matrix-doc/pull/2346 is in spec
            "type": str(StateHalfShotBridge),
            "state_key": self.bridge_info_state_key,
            "content": self.bridge_info,
        }]
        if self.config["bridge.encryption.default"] and self.matrix.e2ee:
            self.encrypted = True
            initial_state.append({
                "type": str(EventType.ROOM_ENCRYPTION),
                "content": {"algorithm": str(EncryptionAlgorithm.MEGOLM_V1)},
            })
            if self.is_direct:
                invites.append(self.az.bot_mxid)

        creation_content = {}
        if not self.config["bridge.federate_rooms"]:
            creation_content["m.federate"] = False
        self.mxid = await self.main_intent.create_room(
            name=self.name if self.encrypted or not self.is_direct else None,
            is_direct=self.is_direct,
            initial_state=initial_state,
            invitees=invites,
            creation_content=creation_content,
        )
        if not self.mxid:
            raise Exception("Failed to create room: no mxid returned")
        if self.encrypted and self.matrix.e2ee and self.is_direct:
            try:
                await self.az.intent.ensure_joined(self.mxid)
            except Exception:
                self.log.warning(f"Failed to add bridge bot to new private chat {self.mxid}")
        await self.save()
        self.log.debug(f"Matrix room created: {self.mxid}")
        self.by_mxid[self.mxid] = self
        await self._update_participants(source, info)

        puppet = await p.Puppet.get_by_custom_mxid(source.mxid)
        await self.main_intent.invite_user(self.mxid, source.mxid,
                                           extra_content=self._get_invite_content(puppet))
        if puppet:
            try:
                if self.is_direct:
                    await source.update_direct_chats({self.main_intent.mxid: [self.mxid]})
                await puppet.intent.join_room_by_id(self.mxid)
            except MatrixError:
                self.log.debug("Failed to join custom puppet into newly created portal",
                               exc_info=True)

        asyncio.create_task(self.backfill(source, is_initial=True))

        return self.mxid

    # endregion
    # region Matrix event handling

    def require_send_lock(self, user_id: str) -> asyncio.Lock:
        try:
            lock = self._send_locks[user_id]
        except KeyError:
            lock = asyncio.Lock()
            self._send_locks[user_id] = lock
        return lock

    def optional_send_lock(self, user_id: str) -> Union[asyncio.Lock, FakeLock]:
        try:
            return self._send_locks[user_id]
        except KeyError:
            pass
        return self._noop_lock

    async def _send_delivery_receipt(self, event_id: EventID) -> None:
        if event_id and self.config["bridge.delivery_receipts"]:
            try:
                await self.az.intent.mark_read(self.mxid, event_id)
            except Exception:
                self.log.exception("Failed to send delivery receipt for %s", event_id)

    async def handle_matrix_reaction(self, sender: 'u.User', reaction_id: EventID,
                                     target_id: EventID, reaction: str) -> None:
        reaction = reaction.rstrip("\ufe0f")

        target = await DBMessage.get_by_mxid(target_id, self.mxid)
        if not target:
            self._rec_dropped(sender, reaction_id, EventType.REACTION,
                              reason="reaction target not found")
            return
        existing = await DBReaction.get_by_gcid(reaction, sender.gcid, target.gcid,
                                                target.gc_chat, target.gc_receiver)
        if existing:
            self._rec_dropped(sender, reaction_id, EventType.REACTION, reason="duplicate reaction")
            return
        # TODO real timestamp?
        fake_ts = int(time.time() * 1000)
        # TODO proper locks?
        await DBReaction(mxid=reaction_id, mx_room=self.mxid, emoji=reaction,
                         gc_sender=sender.gcid, gc_msgid=target.gcid, gc_chat=target.gc_chat,
                         gc_receiver=target.gc_receiver, timestamp=fake_ts).insert()
        try:
            await sender.client.react(target.gc_chat, target.gc_parent_id, target.gcid, reaction)
        except Exception as e:
            self._rec_error(sender, e, reaction_id, EventType.REACTION)
        else:
            await self._rec_success(sender, reaction_id, EventType.REACTION)

    async def handle_matrix_redaction(self, sender: 'u.User', target_id: EventID,
                                      redaction_id: EventID) -> None:
        target = await DBMessage.get_by_mxid(target_id, self.mxid)
        if target:
            await target.delete()
            try:
                await sender.client.delete_message(target.gc_chat, target.gc_parent_id,
                                                   target.gcid)
            except Exception as e:
                self._rec_error(sender, e, redaction_id, EventType.ROOM_REDACTION)
            else:
                await self._rec_success(sender, redaction_id, EventType.ROOM_REDACTION)
            return

        reaction = await DBReaction.get_by_mxid(target_id, self.mxid)
        if reaction:
            reaction_target = await DBMessage.get_by_gcid(reaction.gc_msgid, reaction.gc_chat,
                                                          reaction.gc_receiver)
            await reaction.delete()
            try:
                await sender.client.react(reaction.gc_chat, reaction_target.gc_parent_id,
                                          reaction_target.gcid, reaction.emoji, remove=True)
            except Exception as e:
                self._rec_error(sender, e, redaction_id, EventType.ROOM_REDACTION)
            else:
                await self._rec_success(sender, redaction_id, EventType.ROOM_REDACTION)
            return

        self._rec_dropped(sender, redaction_id, EventType.ROOM_REDACTION,
                          reason="redaction target not found")

    async def handle_matrix_edit(self, sender: 'u.User', message: MessageEventContent,
                                 event_id: EventID) -> None:
        target = await DBMessage.get_by_mxid(message.get_edit(), self.mxid)
        if not target:
            self._rec_dropped(sender, event_id, EventType.ROOM_MESSAGE,
                              reason="unknown edit target", msgtype=message.msgtype)
            return
        # We don't support non-text edits yet
        if message.msgtype != MessageType.TEXT:
            self._rec_dropped(sender, event_id, EventType.ROOM_MESSAGE, reason="non-text edit",
                              msgtype=message.msgtype)
            return

        text, annotations = await fmt.matrix_to_googlechat(message)
        try:
            async with self.require_send_lock(sender.gcid):
                resp = await sender.client.edit_message(
                    target.gc_chat, target.gc_parent_id, target.gcid,
                    text=text, annotations=annotations,
                )
                self._edit_dedup[target.gcid] = resp.message.last_edit_time
        except Exception as e:
            self._rec_error(sender, e, event_id, EventType.ROOM_MESSAGE, message.msgtype)
        else:
            await self._rec_success(sender, event_id, EventType.ROOM_MESSAGE, message.msgtype)

    async def handle_matrix_message(self, sender: 'u.User', message: MessageEventContent,
                                    event_id: EventID) -> None:
        if message.get_edit():
            await self.handle_matrix_edit(sender, message, event_id)
            return
        reply_to = await DBMessage.get_by_mxid(message.get_reply_to(), self.mxid)
        thread_id = ((reply_to.gc_parent_id or reply_to.gcid)
                     if reply_to and self.is_threaded else None)
        local_id = f"mautrix-googlechat%{random.randint(0, 0xffffffffffffffff)}"
        self._local_dedup.add(local_id)

        # TODO this probably isn't nice for bridging images, it really only needs to lock the
        #      actual message send call and dedup queue append.
        async with self.require_send_lock(sender.gcid):
            try:
                if message.msgtype == MessageType.TEXT or message.msgtype == MessageType.NOTICE:
                    resp = await self._handle_matrix_text(sender, message, thread_id, local_id)
                elif message.msgtype.is_media:
                    resp = await self._handle_matrix_media(sender, message, thread_id, local_id)
                else:
                    raise ValueError(f"Unsupported msgtype {message.msgtype}")
            except Exception as e:
                self._rec_error(sender, e, event_id, EventType.ROOM_MESSAGE, message.msgtype)
            else:
                self.log.debug(f"Handled Matrix message {event_id} -> {local_id} -> {resp.gcid}")
                await self._rec_success(sender, event_id, EventType.ROOM_MESSAGE, message.msgtype)
                self._dedup.appendleft(resp.gcid)
                self._local_dedup.remove(local_id)
                await DBMessage(mxid=event_id, mx_room=self.mxid, gcid=resp.gcid,
                                gc_chat=self.gcid, gc_receiver=self.gc_receiver,
                                gc_parent_id=thread_id, index=0, timestamp=resp.timestamp // 1000,
                                msgtype=message.msgtype.value, gc_sender=sender.gcid).insert()
                self._last_bridged_mxid = event_id

    def _rec_dropped(self, user: 'u.User', event_id: EventID, evt_type: EventType, reason: str,
                     msgtype: Optional[MessageType] = None) -> None:
        user.send_remote_checkpoint(
            status=MessageSendCheckpointStatus.PERM_FAILURE,
            event_id=event_id,
            room_id=self.mxid,
            event_type=evt_type,
            message_type=msgtype,
            error=Exception(reason),
        )

    def _rec_error(self, user: 'u.User', err: Exception, event_id: EventID, evt_type: EventType,
                   msgtype: Optional[MessageType] = None, edit: bool = False) -> None:
        if evt_type == EventType.ROOM_MESSAGE:
            if edit:
                self.log.exception(f"Failed handling Matrix edit {event_id}", exc_info=err)
            else:
                self.log.exception(f"Failed handling Matrix message {event_id}", exc_info=err)
        elif evt_type == EventType.ROOM_REDACTION:
            self.log.exception(f"Failed handling Matrix redaction {event_id}", exc_info=err)
        elif evt_type == EventType.REACTION:
            self.log.exception(f"Failed handling Matrix reaction {event_id}", exc_info=err)
        else:
            self.log.exception(f"Failed handling unknown Matrix event {event_id}", exc_info=err)
        user.send_remote_checkpoint(
            status=MessageSendCheckpointStatus.PERM_FAILURE,
            event_id=event_id,
            room_id=self.mxid,
            event_type=evt_type,
            message_type=msgtype,
            error=err,
        )

    async def _rec_success(self, user: 'u.User', event_id: EventID, evt_type: EventType,
                           msgtype: Optional[MessageType] = None) -> None:
        await self._send_delivery_receipt(event_id)
        _ = user.send_remote_checkpoint(
            status=MessageSendCheckpointStatus.SUCCESS,
            event_id=event_id,
            room_id=self.mxid,
            event_type=evt_type,
            message_type=msgtype,
        )

    @staticmethod
    def _get_send_response(resp: Union[googlechat.CreateTopicResponse,
                                       googlechat.CreateMessageResponse]) -> SendResponse:
        if isinstance(resp, googlechat.CreateTopicResponse):
            return SendResponse(gcid=resp.topic.id.topic_id, timestamp=resp.topic.create_time_usec)
        return SendResponse(gcid=resp.message.id.message_id, timestamp=resp.message.create_time)

    async def _handle_matrix_text(self, sender: 'u.User', message: TextMessageEventContent,
                                  thread_id: str, local_id: str) -> SendResponse:
        text, annotations = await fmt.matrix_to_googlechat(message)
        await sender.set_typing(self.gcid, typing=False)
        resp = await sender.client.send_message(self.gcid, text=text, annotations=annotations,
                                                thread_id=thread_id, local_id=local_id)
        return self._get_send_response(resp)

    async def _handle_matrix_media(self, sender: 'u.User', message: MediaMessageEventContent,
                                   thread_id: str, local_id: str) -> SendResponse:
        if message.file and decrypt_attachment:
            data = await self.main_intent.download_media(message.file.url)
            data = decrypt_attachment(data, message.file.key.key,
                                      message.file.hashes.get("sha256"), message.file.iv)
        elif message.url:
            data = await self.main_intent.download_media(message.url)
        else:
            raise Exception("Failed to download media from matrix")
        mime = message.info.mimetype or magic.from_buffer(data, mime=True)
        upload = await sender.client.upload_file(data=data, group_id=self.gcid_plain,
                                                 filename=message.body, mime_type=mime)
        annotations = [googlechat.Annotation(
            type=googlechat.UPLOAD_METADATA,
            upload_metadata=upload,
            chip_render_type=googlechat.Annotation.RENDER,
        )]
        resp = await sender.client.send_message(self.gcid, annotations=annotations,
                                                thread_id=thread_id, local_id=local_id)
        return self._get_send_response(resp)

    async def handle_matrix_leave(self, user: 'u.User') -> None:
        if self.is_direct:
            self.log.info(f"{user.mxid} left private chat portal with {self.gcid},"
                          " cleaning up and deleting...")
            await self.cleanup_and_delete()
        else:
            self.log.debug(f"{user.mxid} left portal to {self.gcid}")

    async def handle_matrix_typing(self, users: Set[UserID]) -> None:
        user_map = {mxid: await u.User.get_by_mxid(mxid, create=False) for mxid in users}
        stopped_typing = [user_map[mxid].set_typing(self.gcid, False)
                          for mxid in self._typing - users
                          if user_map.get(mxid)]
        started_typing = [user_map[mxid].set_typing(self.gcid, True)
                          for mxid in users - self._typing
                          if user_map.get(mxid)]
        self._typing = users
        await asyncio.gather(*stopped_typing, *started_typing)

    # endregion
    # region Hangouts event handling

    async def _bridge_own_message_pm(self, source: 'u.User', sender: 'p.Puppet', msg_id: str,
                                     invite: bool = True) -> bool:
        if self.is_direct and sender.gcid == source.gcid and not sender.is_real_user:
            if self.invite_own_puppet_to_pm and invite:
                await self.main_intent.invite_user(self.mxid, sender.mxid)
            elif (await self.az.state_store.get_membership(self.mxid, sender.mxid)
                  != Membership.JOIN):
                self.log.warning(f"Ignoring own {msg_id} in private chat "
                                 "because own puppet is not in room.")
                return False
        return True

    async def handle_googlechat_reaction(self, evt: googlechat.MessageReactionEvent) -> None:
        if not self.mxid:
            return
        sender = await p.Puppet.get_by_gcid(evt.user_id.id)
        target = await DBMessage.get_by_gcid(evt.message_id.message_id, self.gcid,
                                             self.gc_receiver)
        if not target:
            self.log.debug(f"Dropping reaction to unknown message {evt.message_id}")
            return
        existing = await DBReaction.get_by_gcid(evt.emoji.unicode, sender.gcid, target.gcid,
                                                target.gc_chat, target.gc_receiver)
        if evt.type == googlechat.MessageReactionEvent.ADD:
            if existing:
                # Duplicate reaction
                return
            timestamp = evt.timestamp // 1000
            matrix_reaction = evt.emoji.unicode
            # TODO there are probably other emojis that need variation selectors
            #      mautrix-facebook also needs improved logic for this, so put it in mautrix-python
            if matrix_reaction in ("\u2764", "\U0001f44d", "\U0001f44e"):
                matrix_reaction += "\ufe0f"
            event_id = await sender.intent_for(self).react(target.mx_room, target.mxid,
                                                           matrix_reaction, timestamp=timestamp)
            await DBReaction(mxid=event_id, mx_room=target.mx_room, emoji=evt.emoji.unicode,
                             gc_sender=sender.gcid, gc_msgid=target.gcid, gc_chat=target.gc_chat,
                             gc_receiver=target.gc_receiver, timestamp=timestamp).insert()
        elif evt.type == googlechat.MessageReactionEvent.REMOVE:
            if not existing:
                # Non-existent reaction
                return
            try:
                await sender.intent_for(self).redact(existing.mx_room, existing.mxid)
            except MForbidden:
                await self.main_intent.redact(existing.mx_room, existing.mxid)
            finally:
                await existing.delete()
        else:
            self.log.debug(f"Unknown reaction event type {evt.type}")

    async def handle_googlechat_redaction(self, evt: googlechat.MessageDeletedEvent) -> None:
        if not self.mxid:
            return
        target = await DBMessage.get_all_by_gcid(evt.message_id.message_id, self.gcid,
                                                 self.gc_receiver)
        if not target:
            self.log.debug(f"Dropping deletion of unknown message {evt.message_id}")
            return
        for msg in target:
            await msg.delete()
            try:
                await self.main_intent.redact(msg.mx_room, msg.mxid,
                                              timestamp=evt.timestamp // 1000)
            except Exception as e:
                self.log.warning(f"Failed to redact {msg.mxid}: {e}")

    async def handle_googlechat_edit(self, source: 'u.User', evt: googlechat.Message) -> None:
        if not self.mxid:
            return
        sender = await p.Puppet.get_by_gcid(evt.creator.user_id.id)
        msg_id = evt.id.message_id
        if not await self._bridge_own_message_pm(source, sender, f"edit {msg_id}"):
            return
        async with self.optional_send_lock(sender.gcid):
            edit_ts = evt.last_edit_time or evt.last_update_time
            try:
                if self._edit_dedup[msg_id] >= edit_ts:
                    self.log.debug(f"Ignoring likely duplicate edit of {msg_id} at {edit_ts}")
                    return
            except KeyError:
                pass
            self._edit_dedup[msg_id] = edit_ts
        target = await DBMessage.get_by_gcid(msg_id, self.gcid, self.gc_receiver, index=0)
        if not target:
            self.log.debug(f"Ignoring edit of unknown message {msg_id}")
            return
        elif target.msgtype != "m.text" or not evt.text_body:
            # Figuring out how to map multipart message edits to Matrix is hard, so don't even try
            self.log.debug(f"Ignoring edit of non-text message {msg_id}")
            return

        content = await fmt.googlechat_to_matrix(evt.text_body, evt.annotations)
        content.set_edit(target.mxid)
        event_id = await self._send_message(sender.intent_for(self), content,
                                            timestamp=edit_ts // 1000)
        self.log.debug("Handled Google Chat edit of %s at %s -> %s", msg_id, edit_ts, event_id)
        await self._send_delivery_receipt(event_id)

    async def handle_googlechat_message(self, source: 'u.User', evt: googlechat.Message) -> None:
        sender = await p.Puppet.get_by_gcid(evt.creator.user_id.id)
        msg_id = evt.id.message_id
        async with self.optional_send_lock(sender.gcid):
            if evt.local_id in self._local_dedup:
                self.log.debug(f"Dropping message {msg_id} (found in local dedup set)")
                return
            elif msg_id in self._dedup:
                self.log.debug(f"Dropping message {msg_id} (found in dedup queue)")
                return
            self._dedup.appendleft(msg_id)
        if not self.mxid:
            mxid = await self.create_matrix_room(source)
            if not mxid:
                # Failed to create
                return
        if not await self._bridge_own_message_pm(source, sender, f"message {msg_id}"):
            return
        intent = sender.intent_for(self)
        self.log.debug("Handling Google Chat message %s", msg_id)

        # Google Chat timestamps are in microseconds, Matrix wants milliseconds
        timestamp = evt.create_time // 1000
        reply_to = None
        parent_id = evt.id.parent_id.topic_id.topic_id
        if parent_id:
            reply_to = await DBMessage.get_last_in_thread(parent_id, self.gcid, self.gc_receiver)

        event_ids: List[Tuple[EventID, MessageType]] = []
        if evt.text_body:
            content = await fmt.googlechat_to_matrix(evt.text_body, evt.annotations)
            if reply_to:
                content.set_reply(reply_to.mxid)
            event_id = await self._send_message(intent, content, timestamp=timestamp)
            event_ids.append((event_id, MessageType.TEXT))
        attachment_urls = self._get_urls_from_annotations(evt.annotations)
        if attachment_urls:
            try:
                async for event_id, msgtype in self.process_googlechat_attachments(
                    source, attachment_urls, intent, reply_to=reply_to, timestamp=timestamp,
                ):
                    event_ids.append((event_id, msgtype))
            except Exception:
                self.log.exception("Failed to process attachments")
        if not event_ids:
            # TODO send notification
            self.log.debug("Unhandled Google Chat message %s", msg_id)
            return
        for index, (event_id, msgtype) in enumerate(event_ids):
            await DBMessage(mxid=event_id, mx_room=self.mxid, gcid=msg_id, gc_chat=self.gcid,
                            gc_receiver=self.gc_receiver, gc_parent_id=parent_id,
                            index=index, timestamp=timestamp, msgtype=msgtype.value,
                            gc_sender=sender.gcid).insert()
        self.log.debug("Handled Google Chat message %s -> %s", msg_id, event_ids)
        await self._send_delivery_receipt(event_ids[-1][0])

    @staticmethod
    async def _download_external_attachment(url: URL, max_size: int) -> Tuple[bytes, str, str]:
        async with aiohttp.ClientSession() as sess, sess.get(url) as resp:
            resp.raise_for_status()
            filename = url.path.split("/")[-1]
            if 0 < max_size < int(resp.headers.get("Content-Length", "0")):
                raise FileTooLargeError("Image size larger than maximum")
            blocks = []
            while True:
                block = await resp.content.read(max_size)
                if not block:
                    break
                max_size -= len(block)
                blocks.append(block)
            data = b"".join(blocks)
            mime = resp.headers.get("Content-Type") or magic.from_buffer(data, mime=True)
            return data, mime, filename

    @staticmethod
    def _get_urls_from_annotations(annotations: List[googlechat.Annotation]) -> List[URL]:
        if not annotations:
            return []
        attachment_urls = []
        for annotation in annotations:
            if annotation.HasField('upload_metadata'):
                attachment_urls.append(
                    URL("https://chat.google.com/api/get_attachment_url").with_query({
                        "url_type": "FIFE_URL",
                        "attachment_token": annotation.upload_metadata.attachment_token,
                    })
                )
            elif annotation.HasField('url_metadata'):
                if annotation.url_metadata.should_not_render:
                    continue
                if annotation.url_metadata.HasField('image_url'):
                    attachment_urls.append(URL(annotation.url_metadata.image_url))
                elif annotation.url_metadata.HasField('url'):
                    attachment_urls.append(URL(annotation.url_metadata.url.url))
        return attachment_urls

    async def process_googlechat_attachments(self, source: 'u.User', urls: List[URL],
                                             intent: IntentAPI, reply_to: DBMessage, timestamp: int
                                             ) -> AsyncIterable[Tuple[EventID, MessageType]]:
        max_size = self.matrix.media_config.upload_size
        for url in urls:
            try:
                if url.host.endswith(".google.com"):
                    data, mime, filename = await source.client.download_attachment(url, max_size)
                else:
                    data, mime, filename = await self._download_external_attachment(url, max_size)
            except FileTooLargeError:
                # TODO send error message
                self.log.warning("Can't upload too large attachment")
                continue
            except aiohttp.ClientResponseError as e:
                self.log.warning(f"Failed to download attachment: {e}")
                continue

            msgtype = getattr(MessageType, mime.split("/")[0].upper(), MessageType.FILE)
            if msgtype == MessageType.TEXT:
                msgtype = MessageType.FILE
            if not filename or filename == "get_attachment_url":
                filename = msgtype.value + mimetypes.guess_extension(mime)
            upload_mime = mime
            decryption_info = None
            if self.encrypted and encrypt_attachment:
                data, decryption_info = encrypt_attachment(data)
                upload_mime = "application/octet-stream"
            mxc_url = await intent.upload_media(data, mime_type=upload_mime, filename=filename)
            if decryption_info:
                decryption_info.url = mxc_url
                mxc_url = None
            content = MediaMessageEventContent(url=mxc_url, file=decryption_info, body=filename,
                                               info=ImageInfo(size=len(data), mimetype=mime))
            content.msgtype = msgtype
            if reply_to:
                content.set_reply(reply_to.mxid)
            event_id = await self._send_message(intent, content, timestamp=timestamp)
            yield event_id, content.msgtype

    async def handle_googlechat_read_receipts(self, evt: googlechat.ReadReceiptChangedEvent
                                              ) -> None:
        rr: googlechat.ReadReceipt
        for rr in evt.read_receipt_set.read_receipts:
            await self.mark_read(rr.user.user_id.id, rr.read_time_micros)

    async def mark_read(self, user_id: str, ts: int) -> None:
        message = await DBMessage.get_closest_before(self.gcid, self.gc_receiver, ts // 1000)
        puppet = await p.Puppet.get_by_gcid(user_id)
        if puppet and message:
            await puppet.intent_for(self).mark_read(message.mx_room, message.mxid)

    async def handle_googlechat_typing(self, source: 'u.User', sender: str, status: int) -> None:
        if not self.mxid:
            return
        puppet = await p.Puppet.get_by_gcid(sender)
        if self.is_direct and puppet.gcid == source.gcid:
            membership = await self.az.state_store.get_membership(self.mxid, puppet.mxid)
            if membership != Membership.JOIN:
                return
        await puppet.intent_for(self).set_typing(self.mxid, status == googlechat.TYPING,
                                                 timeout=6000)

    # endregion
    # region Getters

    async def postinit(self) -> None:
        self.by_gcid[self.gcid_full] = self
        if self.mxid:
            self.by_mxid[self.mxid] = self
        if self.other_user_id or not self.is_direct:
            self._main_intent = (
                (await p.Puppet.get_by_gcid(self.other_user_id)).default_mxid_intent
                if self.is_direct else self.az.intent
            )

    @classmethod
    @async_getter_lock
    async def get_by_mxid(cls, mxid: RoomID) -> Optional['Portal']:
        try:
            return cls.by_mxid[mxid]
        except KeyError:
            pass

        portal = cast(cls, await super().get_by_mxid(mxid))
        if portal:
            await portal.postinit()
            return portal

        return None

    @classmethod
    @async_getter_lock
    async def get_by_gcid(cls, gcid: str, receiver: Optional[str] = None) -> Optional['Portal']:
        receiver = "" if gcid.startswith("space:") else receiver
        try:
            return cls.by_gcid[(gcid, receiver)]
        except KeyError:
            pass

        portal = cast(cls, await super().get_by_gcid(gcid, receiver))
        if portal:
            await portal.postinit()
            return portal

        portal = cls(gcid=gcid, gc_receiver=receiver)
        await portal.insert()
        await portal.postinit()
        return portal

    @classmethod
    async def get_all_by_receiver(cls, receiver: str) -> AsyncIterable['Portal']:
        portal: Portal
        for portal in await super().get_all_by_receiver(receiver):
            try:
                yield cls.by_gcid[(portal.gcid, portal.gc_receiver)]
            except KeyError:
                await portal.postinit()
                yield portal

    @classmethod
    async def all(cls) -> AsyncIterable['Portal']:
        portal: Portal
        for portal in await super().all():
            try:
                yield cls.by_gcid[(portal.gcid, portal.gc_receiver)]
            except KeyError:
                await portal.postinit()
                yield portal

    # endregion
