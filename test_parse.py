import json

str_json = """{
  "last_scanned_block": 17987494,
  "blocks": {
    "17986230": {
      "0xba963f6a4e9619fac1faf66009e3ad7b5d3237bfff7a8b82da2762a99a7bcc56": {
        "344": {
          "from": "0x7B95Ec873268a6BFC6427e7a28e396Db9D0ebc65",
          "to": "0x94E61aeA6aD9F699c9C7572B1a2E62661FeD98B6",
          "value": 27796315810770500972682,
          "timestamp": "2023-08-24T18:04:35"
        }
      }
    },
    "17986256": {
      "0x923459c08a274286c77402851ba4a332deaf913e503082d50d29d2a49b694117": {
        "213": {
          "from": "0x94E61aeA6aD9F699c9C7572B1a2E62661FeD98B6",
          "to": "0x38F5E5b4DA37531a6e85161e337e0238bB27aa90",
          "value": 42287324927037809009,
          "timestamp": "2023-08-24T18:09:47"
        },
        "214": {
          "from": "0x94E61aeA6aD9F699c9C7572B1a2E62661FeD98B6",
          "to": "0x22F9dCF4647084d6C31b2765F6910cd85C178C18",
          "value": 27754028485843463163673,
          "timestamp": "2023-08-24T18:09:47"
        },
        "215": {
          "from": "0x22F9dCF4647084d6C31b2765F6910cd85C178C18",
          "to": "0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14",
          "value": 27754028485843463163673,
          "timestamp": "2023-08-24T18:09:47"
        }
      }
    }
  }
}"""

data = json.loads(str_json)
start_balance = 0
for block_number in data['blocks']:
        for tx_hash in data['blocks'][block_number]:
                for event_number in data['blocks'][block_number][tx_hash]:
                    addr_target = "0x94E61aeA6aD9F699c9C7572B1a2E62661FeD98B6"
                    addr_from = data['blocks'][block_number][tx_hash][event_number]['from']
                    addr_to = data['blocks'][block_number][tx_hash][event_number]['to']
                    value = data['blocks'][block_number][tx_hash][event_number]['value']
                    if(addr_from == addr_target):
                        start_balance_before = start_balance
                        start_balance = start_balance_before - value
                        print(data['blocks'][block_number][tx_hash][event_number]['from'], "-", value, "=", start_balance)
                    if( addr_to == addr_target):
                        start_balance_before = start_balance
                        start_balance = start_balance_before + value
                        print(data['blocks'][block_number][tx_hash][event_number]['to'], "+", value, "=", start_balance)

