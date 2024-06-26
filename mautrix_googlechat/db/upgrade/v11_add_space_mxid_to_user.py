# mautrix-googlechat - A Matrix-Google Chat puppeting bridge
# Copyright (C) 2023 Tulir Asokan, Sumner Evans
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
from mautrix.util.async_db import Connection

from . import upgrade_table


@upgrade_table.register(description="Add space MXID to User")
async def upgrade_v6(conn: Connection):
    create_table_queries = [
        """
        ALTER TABLE "user" ADD COLUMN space_mxid TEXT
        """,
    ]

    for query in create_table_queries:
        await conn.execute(query)