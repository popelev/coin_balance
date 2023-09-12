# import datetime
import uvicorn
import json
from pydantic import BaseModel, ValidationError
from typing import List, Optional

import asyncio
import motor.motor_asyncio

from fastapi.logger import logger
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import logging

from envparse import Env

from web3 import AsyncWeb3, AsyncHTTPProvider

import os

from event_filter import filter

gunicorn_logger = logging.getLogger('gunicorn.error')
logger.handlers = gunicorn_logger.handlers
    
app = FastAPI()

DEFAULT_RPC = "https://mainnet.infura.io/v3/1df40ac1020e4a9083b81e1e7c6892be"
DEFAULT_MONGO = "mongodb://localhost:27017/local"

env = Env()
MONGODB_URL = env.str("MONGODB_URL", default=DEFAULT_MONGO)
RPC = env.str("RPC", default=DEFAULT_RPC)

 
# Load ERC20 token ABI from file
abi_file = open('abi.json')
abi = json.load(abi_file)


class Balance(BaseModel):
    amount: str
    # datetime: datetime

class Wallet(BaseModel):
    id: int
    address: str
    actual_eth_balance: str
    actual_token_balance: str
    token_history: Optional[List[Balance]] = []

@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": exc.errors()}),
    )
    
@app.get("/")
async def mainpage() -> str:
    return "YOU ARE ON THE MAIN PAGE! GO TO /docs"

@app.get("/pingRPC")
async def ping() -> dict:
    web3 = instanciate_w3(RPC)
    return {"Success": {await web3.is_connected()}}
   
# user_address = "0x7a16ff8270133f063aab6c9977183d9e72835428" 
# contract_address = "0xD533a949740bb3306d119CC777fa900bA034cd52" 

@app.get("/address/{user_address, contract_address}")
async def balance_of_token(request: Request, user_address: str, contract_address: str,):
    web3 = instanciate_w3(RPC)
    eth_Balance = await web3.eth.get_balance(to_checksum(user_address))
    contract = web3.eth.contract(to_checksum(contract_address), abi=abi)
    token_balance = await contract.functions.balanceOf(to_checksum(user_address)).call()
    return {"Success": True,
            "Wallet": str(user_address),
            "Eth Balance in Wai": str(eth_Balance),
            "Token Balance in Wai": str(token_balance) }

@app.post("/address/{contract_address}")
async def contract_token_events(request: Request, contract_address: str,):
    # logger.setLevel(logging.DEBUG)
    web3 = instanciate_w3(RPC)
    client = motor.motor_asyncio.AsyncIOMotorClient(DEFAULT_MONGO, serverSelectionTimeoutMS=5000)
    
    database = client.testdb
    
    filter_success = await filter(database, web3, contract_address)    
    
    return {"Success": filter_success,
            "Contract": str(contract_address)}

# @app.get("/address")
# async def get_records(request: Request) -> list:
#     # mongo_client: AsyncIOMotorClient = request.app.state.mongo_client["test_database"]
#     # cursor = mongo_client.records.find({})
#     res = []
#     # for document in await cursor.to_list(length=100):
#     #     document["_id"] = str(document["_id"])
#     #     res.append(document)
#     return res

def instanciate_w3(url) -> AsyncWeb3:
    w3_instance = AsyncWeb3(AsyncHTTPProvider(url))
    return w3_instance
def to_checksum(address):
    checksum = AsyncWeb3.to_checksum_address(address.lower())
    return checksum

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
