import asyncio
import hmac
import os
import pickle
import random
import re
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import aioredis
import stripe
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

# Constants

SHARD_PRODUCT_IDS = {
    "prod_I6mX11xoeVPuE6": 500,
    "prod_I6mXA1Bq9xI52b": 1100,
    "prod_I6mX6x18dVqr9D": 2400,
    "prod_I6mXe6caYABny8": 5600,
    "prod_Ja5G3l3d2MnMUU": 15000,
}

MSF_FUNDRAISER_PRODUCT_IDS = ["prod_Kf94LTHSJjqsrP", "prod_Kf94xExNn3icbL"]

MEMBER_CACHE_KEY = "db:member"

# Sightings are deleted this long after the last captcha solve on that network, unless a
# suspension of the account or of the network needs them for longer.
NETWORK_SIGHTING_RETENTION = timedelta(days=90)
# Hex HMAC-SHA256 digest computed by the verify site; raw IPs never reach this server.
NETWORK_HASH = re.compile(r"[0-9a-f]{64}")
HOSTING_TARGET_ID = "hosting"


NATURES = [
    "Adamant",
    "Bashful",
    "Bold",
    "Brave",
    "Calm",
    "Careful",
    "Docile",
    "Gentle",
    "Hardy",
    "Hasty",
    "Impish",
    "Jolly",
    "Lax",
    "Lonely",
    "Mild",
    "Modest",
    "Naive",
    "Naughty",
    "Quiet",
    "Quirky",
    "Rash",
    "Relaxed",
    "Sassy",
    "Serious",
    "Timid",
]

stripe.api_key = os.environ["STRIPE_KEY"]
STRIPE_SECRET = os.environ["STRIPE_SECRET"]
DBL_SECRET = os.environ["DBL_SECRET"]
CAPTCHA_SECRET = os.environ["CAPTCHA_SECRET"]
ACCOUNT_SECRET = os.environ["ACCOUNT_SECRET"]

DATABASE_URI = os.getenv("DATABASE_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME")

if DATABASE_URI is None:
    DATABASE_URI = "mongodb://{}:{}@{}".format(
        quote_plus(os.environ["DATABASE_USERNAME"]),
        quote_plus(os.environ["DATABASE_PASSWORD"]),
        os.environ["DATABASE_HOST"],
    )

REDIS_CONF = {
    "address": os.environ["REDIS_URI"],
    "password": os.getenv("REDIS_PASSWORD"),
}


# Setup

redis = None
client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]


middleware = [Middleware(CORSMiddleware, allow_origins=["*"])]
app = Starlette(debug=os.getenv("DEBUG", False), middleware=middleware)


# Routes


@app.on_event("startup")
async def startup():
    global redis
    redis = await aioredis.create_redis_pool(**REDIS_CONF)
    await create_network_sighting_indexes()


async def create_network_sighting_indexes():
    sightings = db.network_sightings
    await sightings.create_index([("user_id", ASCENDING), ("ip_hash", ASCENDING)], unique=True)
    await sightings.create_index([("user_id", ASCENDING), ("last_seen", DESCENDING)])
    await sightings.create_index("ip_hash")
    await sightings.create_index("subnet_hash")
    await sightings.create_index("asn")
    await sightings.create_index("expires_at", expireAfterSeconds=0)


@app.route("/")
def hello(_request):
    return PlainTextResponse("Hello!")


@app.route("/stats")
async def stats(_request):
    servers = await db.stats.aggregate([{"$group": {"_id": None, "servers": {"$sum": "$servers"}}}]).to_list(1)
    users = await db.member.estimated_document_count()
    return JSONResponse({"servers": servers[0]["servers"], "users": users})


async def process_vote(request, provider, uid_key):
    if request.headers["authorization"] != DBL_SECRET:
        return PlainTextResponse("Invalid Secret", 401)

    json = await request.json()
    now = datetime.utcnow()
    uid = int(json[uid_key])

    res = await db.member.find_one({"_id": uid})
    if res is None:
        return PlainTextResponse("Invalid User", 404)

    streak = res.get("vote_streak", 0) + 1

    if streak >= 40 and streak % 10 == 0:
        box_type = "master"
    elif streak >= 14:
        box_type = "ultra"
    elif streak >= 7:
        box_type = "great"
    else:
        box_type = "normal"

    await db.member.update_one(
        {"_id": uid},
        {
            "$set": {
                "vote_streak": streak,
                f"last_voted_on.{provider}": now,
                f"need_vote_reminder_on.{provider}": True,
            },
            "$inc": {"vote_total": 1, f"gifts_{box_type}": 1},
        },
    )
    await redis.hdel(f"db:member", uid)

    return PlainTextResponse("Success")


@app.route("/topgg", methods=["POST"])
async def topgg(request):
    return await process_vote(request, "topgg", "user")


@app.route("/dbl", methods=["POST"])
async def dbl(request):
    return await process_vote(request, "dbl", "id")


def get_checkout_item(id):
    return stripe.checkout.Session.list_line_items(id, limit=1).data[0]


@app.route("/stripe", methods=["POST"])
async def stripe_webhook(request):
    try:
        event = stripe.Webhook.construct_event(
            await request.body(),
            request.headers["stripe-signature"],
            STRIPE_SECRET,
        )
    except ValueError:
        return PlainTextResponse("Invalid Payload", 400)
    except stripe.error.SignatureVerificationError:
        return PlainTextResponse("Invalid Signature", 401)

    if event.type != "checkout.session.completed":
        return PlainTextResponse("Invalid Event", 400)

    session = event.data.object
    loop = asyncio.get_event_loop()
    item = await loop.run_in_executor(None, get_checkout_item, session.id)

    uid = int(session.metadata.id)

    if shards := SHARD_PRODUCT_IDS.get(item.price.product):
        await db.member.update_one({"_id": uid}, {"$inc": {"premium_balance": shards}})
        await redis.hdel(f"db:member", uid)
        await redis.rpush(
            "send_dm",
            pickle.dumps((uid, f"Thanks for donating! You received **{shards}** shards.")),
        )

        return PlainTextResponse("Success")

    elif item.price.product in MSF_FUNDRAISER_PRODUCT_IDS:
        await client.support.fundraiser.update_one(
            {"_id": "msf_2021"}, {"$inc": {"value": item.price.unit_amount}}, upsert=True
        )
        await db.member.update_one({"_id": uid}, {"$inc": {"msf_donated_amount": item.price.unit_amount}})

        pokemon = []
        for _ in range(item.price.unit_amount // 1500):
            shiny = random.randint(1, 4096) == 1
            level = min(max(int(random.normalvariate(20, 10)), 1), 100)
            ivs = [random.randint(0, 31) for _ in range(6)]
            pokemon.append(
                {
                    "owner_id": uid,
                    "owned_by": "user",
                    "species_id": 50032,
                    "level": level,
                    "xp": 0,
                    "nature": random.choice(NATURES),
                    "iv_hp": ivs[0],
                    "iv_atk": ivs[1],
                    "iv_defn": ivs[2],
                    "iv_satk": ivs[3],
                    "iv_sdef": ivs[4],
                    "iv_spd": ivs[5],
                    "iv_total": sum(ivs),
                    "moves": [],
                    "shiny": shiny,
                    "idx": await fetch_next_idx(uid),
                }
            )

        if len(pokemon) > 0:
            await db.pokemon.insert_many(pokemon)

        msg = f"Thanks for contributing **${item.price.unit_amount / 100:.2f}** to our Thanksgiving fundraiser for Médecins Sans Frontières / Doctors Without Borders! Your charitable donation is greatly appreciated. 100% of your donation will be donated to the charity, and Pokétwo will cover all transaction fees associated with the payment."

        if len(pokemon) > 0:
            msg += f" You have received **{len(pokemon)} United Pikachu{'s' if len(pokemon) > 1 else ''}**."

        await redis.rpush("send_dm", pickle.dumps((uid, msg)))

        return PlainTextResponse("Success")

    return PlainTextResponse("Error", 400)


async def fetch_next_idx(uid, reserve=1):
    result = await db.member.find_one_and_update(
        {"_id": uid},
        {"$inc": {"next_idx": reserve}},
        projection={"next_idx": 1},
    )
    await redis.hdel(f"db:member", uid)
    return result["next_idx"]


@app.route("/account/{discord_id}", methods=["GET"])
async def account(request):
    if request.headers.get("authorization") != ACCOUNT_SECRET:
        return PlainTextResponse("Invalid Secret", status_code=401)

    try:
        discord_id = int(request.path_params["discord_id"])
    except ValueError:
        return PlainTextResponse("Invalid ID", status_code=400)

    member = await db.member.find_one({"_id": discord_id})
    if member is None:
        return PlainTextResponse("Not Found", status_code=404)

    pokemon_count = await db.pokemon.count_documents({"owner_id": discord_id, "owned_by": "user"})
    return JSONResponse({"balance": member.get("balance", 0), "pokemonCount": pokemon_count})


def is_captcha_secret(value):
    return isinstance(value, str) and hmac.compare_digest(value.encode(), CAPTCHA_SECRET.encode())


def is_network_hash(value):
    return isinstance(value, str) and NETWORK_HASH.fullmatch(value) is not None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_network(data):
    """The payload's `network` field: None if absent, ValueError if malformed."""

    network = data.get("network")
    if network is None:
        return None
    if not isinstance(network, dict):
        raise ValueError
    sighting = {
        "ip_hash": network.get("ip_hash"),
        "subnet_hash": network.get("subnet_hash"),
        "asn": network.get("asn"),
        "key_version": network.get("key_version"),
    }
    if not is_network_hash(sighting["ip_hash"]) or not is_int(sighting["key_version"]):
        raise ValueError
    if sighting["subnet_hash"] is not None and not is_network_hash(sighting["subnet_hash"]):
        raise ValueError
    if sighting["asn"] is not None and not is_int(sighting["asn"]):
        raise ValueError
    return sighting


def network_target_ids(sighting, hosting_asns):
    """Ids of the `network` documents that can suspend this sighting. Matches the bot's format."""

    target_ids = [f"ip:{sighting['ip_hash']}"]
    if sighting["subnet_hash"] is not None:
        target_ids.append(f"subnet:{sighting['subnet_hash']}")
    if sighting["asn"] is not None:
        target_ids.append(f"asn:{sighting['asn']}")
        if sighting["asn"] in hosting_asns:
            target_ids.append(HOSTING_TARGET_ID)
    return target_ids


async def find_network_suspension(sighting, now):
    """Strongest active suspension of the sighting's network: permanent, then latest expiry."""

    hosting = await db.network.find_one({"_id": HOSTING_TARGET_ID}, {"asns": 1})
    target_ids = network_target_ids(sighting, hosting.get("asns", []) if hosting else [])
    suspensions = await db.network.find(
        {
            "_id": {"$in": target_ids},
            "$or": [{"suspended": True}, {"suspended_until": {"$gt": now}}],
        }
    ).to_list(None)
    return max(
        suspensions,
        key=lambda doc: (doc.get("suspended", False), doc.get("suspended_until") or datetime.min),
        default=None,
    )


def member_network_suspension(suspension):
    """Stores the kind of target rather than its id, so hashes aren't copied onto members."""

    return {
        "kind": suspension["_id"].partition(":")[0],
        "reason": suspension.get("suspension_reason"),
        "until": None if suspension.get("suspended") else suspension.get("suspended_until"),
    }


def sighting_expiry(now, *suspensions):
    """When the sighting may be deleted; None keeps it while a permanent suspension is in place."""

    expiry = now + NETWORK_SIGHTING_RETENTION
    for suspension in suspensions:
        if suspension is None:
            continue
        if suspension.get("suspended"):
            return None
        until = suspension.get("suspended_until")
        if until is not None and until > expiry:
            expiry = until
    return expiry


async def record_network_sighting(uid, sighting, now, expires_at):
    update = {
        "$set": {
            "subnet_hash": sighting["subnet_hash"],
            "asn": sighting["asn"],
            "key_version": sighting["key_version"],
            "last_seen": now,
        },
        "$setOnInsert": {"first_seen": now},
        "$inc": {"solves": 1},
    }
    if expires_at is None:
        update["$unset"] = {"expires_at": 1}
    else:
        update["$set"]["expires_at"] = expires_at
    await db.network_sightings.update_one(
        {"user_id": uid, "ip_hash": sighting["ip_hash"]}, update, upsert=True
    )


@app.route("/captcha", methods=["POST"])
async def captcha_webhook(request):
    data = await request.json()

    if not is_captcha_secret(data.get("secret")):
        return PlainTextResponse("Invalid Secret", 401)

    uid = int(data["uid"])
    try:
        sighting = parse_network(data)
    except ValueError:
        return PlainTextResponse("Invalid Network", 400)

    update = {"$set": {"captcha_required": False}, "$inc": {"captchas_solved": 1}}
    network_suspension = None

    if sighting is not None:
        member = await db.member.find_one({"_id": uid}, {"suspended": 1, "suspended_until": 1})
        if member is not None:
            now = datetime.utcnow()
            suspension = await find_network_suspension(sighting, now)
            expires_at = sighting_expiry(now, member, suspension)
            await record_network_sighting(uid, sighting, now, expires_at)
            if suspension is None:
                update["$unset"] = {"network_suspension": 1}
            else:
                network_suspension = member_network_suspension(suspension)
                update["$set"]["network_suspension"] = network_suspension

    await db.member.update_one({"_id": uid}, update)
    await redis.hdel(MEMBER_CACHE_KEY, uid)

    until = network_suspension and network_suspension["until"]
    return JSONResponse(
        {
            "network_suspended": network_suspension is not None,
            "network_suspended_until": until.isoformat() if until else None,
        }
    )
