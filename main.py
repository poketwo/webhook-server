import asyncio
import os
import pickle
import random
from datetime import datetime
from urllib.parse import quote_plus

import aioredis
import stripe
from motor.motor_asyncio import AsyncIOMotorClient
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


@app.route("/captcha", methods=["POST"])
async def captcha_webhook(request):
    data = await request.json()

    if data["secret"] != CAPTCHA_SECRET:
        return PlainTextResponse("Invalid Secret", 401)

    uid = int(data["uid"])

    await redis.hdel("captcha", uid)
    await db.member.update_one({"_id": uid}, {"$inc": {"captchas_solved": 1}})
    return PlainTextResponse("Success")
