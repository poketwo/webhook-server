import pickle
import asyncio
import os
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import aioredis
import stripe
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse


# Constants

PRODUCTS = {
    "prod_I6mX11xoeVPuE6": 500,
    "prod_I6mXA1Bq9xI52b": 1100,
    "prod_I6mX6x18dVqr9D": 2400,
    "prod_I6mXe6caYABny8": 5600,
    "prod_Ja5G3l3d2MnMUU": 15000,
}

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
db = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]
app = Starlette(debug=os.getenv("DEBUG", False))


# Routes


@app.on_event("startup")
async def startup():
    global redis
    redis = await aioredis.create_redis_pool(**REDIS_CONF)


@app.route("/")
def hello(request):
    return PlainTextResponse("Hello!")


@app.route("/dbl", methods=["POST"])
async def dbl(request):
    if request.headers["authorization"] != DBL_SECRET:
        return PlainTextResponse("Invalid Secret", 401)

    json = await request.json()
    now = datetime.utcnow()
    uid = int(json["user"])

    res = await db.member.find_one({"_id": uid})
    if res is None:
        return PlainTextResponse("Invalid User", 404)

    streak = res.get("vote_streak", 0)

    # last_voted = res.get("last_voted", datetime.min)
    # if now - last_voted > timedelta(days=2):
    #     streak = 0

    streak += 1

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
            "$set": {"vote_streak": streak, "last_voted": now, "need_vote_reminder": True},
            "$inc": {"vote_total": 1, f"gifts_{box_type}": 1},
        },
    )
    await redis.hdel(f"db:member", uid)

    # article = "an" if box_type == "ultra" else "a"
    # await redis.rpush(
    #     "send_dm",
    #     pickle.dumps((uid, f"Thanks for voting! You received {article} **{box_type} box**.")),
    # )

    return PlainTextResponse("Success")


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
    shards = PRODUCTS[item.price.product]

    await db.member.update_one({"_id": uid}, {"$inc": {"premium_balance": shards}})
    await redis.hdel(f"db:member", uid)
    await redis.rpush(
        "send_dm", pickle.dumps((uid, f"Thanks for donating! You received **{shards}** shards."))
    )

    return PlainTextResponse("Success")


@app.route("/captcha", methods=["POST"])
async def captcha_webhook(request):
    data = await request.json()

    if data["secret"] != CAPTCHA_SECRET:
        return PlainTextResponse("Invalid Secret", 401)

    uid = int(data["uid"])
    print(uid)

    await redis.hdel("captcha", uid)
    return PlainTextResponse("Success")
