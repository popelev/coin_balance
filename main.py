import uvicorn
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI
from envparse import Env
# from fastapi.routing import APIRoute
from starlette.requests import Request
from web3 import Web3
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

RPC = os.getenv('RPC')
DEFAULT_MONGO = os.getenv('MONGODB_URL')
    
env = Env()
MONGODB_URL = env.str("MONGODB_URL", default=DEFAULT_MONGO)

@app.get("/")
async def mainpage() -> str:
    return "YOU ARE ON THE MAIN PAGE! GO TO /docs"

@app.get("/pingRPC")
async def ping() -> dict:
    web3 = Web3(Web3.HTTPProvider(RPC))
    return {"Success": {web3.is_connected()}}


# class Item(BaseModel):
#     name: str
#     price: float
#     is_offer: Union[bool, None] = None

# @app.post("/records")
# async def create_record(request: Request) -> dict:
#     mongo_client: AsyncIOMotorClient = request.app.state.mongo_client["test_database"]
#     await mongo_client.records.insert_one({"sample": "record"})
#     return {"Success": True}

@app.post("/address")
async def check_address(request: Request) -> dict:
    web3 = Web3(Web3.HTTPProvider(RPC))
    wallet_address = "0x7a16ff8270133f063aab6c9977183d9e72835428" 
    checksum_address = Web3.to_checksum_address(wallet_address)
    balance = web3.eth.get_balance(checksum_address)

    mongo_client: AsyncIOMotorClient = request.app.state.mongo_client["test_database"]
    await mongo_client.records.insert_one({"Wallet": str(wallet_address),
            "Balance in Wai": str(balance)})
    return {"Success": True,
            "Wallet": str(wallet_address),
            "Balance in Wai": str(balance) }

# @app.get("/records")
# async def get_records(request: Request) -> list:
#     mongo_client: AsyncIOMotorClient = request.app.state.mongo_client["test_database"]
#     cursor = mongo_client.records.find({})
#     res = []
#     for document in await cursor.to_list(length=100):
#         document["_id"] = str(document["_id"])
#         res.append(document)
#     return res

@app.get("/address")
async def get_records(request: Request) -> list:
    mongo_client: AsyncIOMotorClient = request.app.state.mongo_client["test_database"]
    cursor = mongo_client.records.find({})
    res = []
    for document in await cursor.to_list(length=100):
        document["_id"] = str(document["_id"])
        res.append(document)
    return res


client = AsyncIOMotorClient(MONGODB_URL)
app.state.mongo_client = client

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
