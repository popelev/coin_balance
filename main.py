# import datetime
import uvicorn
import json
from pydantic import BaseModel, ValidationError
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorClient

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from envparse import Env

from web3 import Web3

import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

RPC = os.getenv('RPC')
DEFAULT_MONGO = os.getenv('MONGODB_URL')
env = Env()
MONGODB_URL = env.str("MONGODB_URL", default=DEFAULT_MONGO)

 
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
    web3 = Web3(Web3.HTTPProvider(RPC))
    return {"Success": {web3.is_connected()}}
   
# user_address = "0x7a16ff8270133f063aab6c9977183d9e72835428" 
# contract_address = "0xD533a949740bb3306d119CC777fa900bA034cd52" 

@app.post("/address/{user_address, contract_address}")
async def check_address(request: Request, user_address: str, contract_address: str,):
    web3 = Web3(Web3.HTTPProvider(RPC))
    eth_Balance = web3.eth.get_balance(Web3.to_checksum_address(user_address))
    contract = web3.eth.contract(Web3.to_checksum_address(contract_address.lower()), abi=abi)
    token_balance = contract.functions.balanceOf(Web3.to_checksum_address(user_address.lower())).call()
    return {"Success": True,
            "Wallet": str(user_address),
            "Eth Balance in Wai": str(eth_Balance),
            "Token Balance in Wai": str(token_balance) }

@app.get("/address/{user_address, contract_address}")
async def balance_of_token(request: Request, user_address: str, contract_address: str,):
    web3 = Web3(Web3.HTTPProvider(RPC))
    eth_Balance = web3.eth.get_balance(Web3.to_checksum_address(user_address))
    contract = web3.eth.contract(Web3.to_checksum_address(contract_address.lower()), abi=abi)
    token_balance = contract.functions.balanceOf(Web3.to_checksum_address(user_address.lower())).call()
    # mongo_client: AsyncIOMotorClient = request.app.state.mongo_client["test_database"]
    # await mongo_client.records.insert_one({"Wallet": str(user_address),
    #         "Eth balance in Wai": str(eth_Balance)})
    return {"Success": True,
            "Wallet": str(user_address),
            "Eth Balance in Wai": str(eth_Balance),
            "Token Balance in Wai": str(token_balance) }


@app.get("/address/{contract_address}")
async def contract_token_events(request: Request, contract_address: str,):
    # TODO
    return {"Success": True,
            "Wallet": str(contract_address),
            "history": ""}

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
