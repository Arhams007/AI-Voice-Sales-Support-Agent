from datetime import datetime
from typing import Dict, Any

VERIFIED_DATA: Dict[str, Dict] = {}
CALL_END_FLAGS: Dict[str, bool] = {}

async def store_customer_data(call_id: str, data_type: str, value: str) -> Dict[str, Any]:
    """
    Store collected customer booking information during the call.
    
    Args:
        call_id: The call identifier
        data_type: Type of data being stored. Use one of: service_type, issue_details, customer_name, preferred_date, home_address
        value: The actual value provided by the customer
        
    Returns:
        Dictionary with success status and confirmation
    """
    VERIFIED_DATA.setdefault(call_id, {})
    VERIFIED_DATA[call_id][data_type] = {
        "value": value,
        "confirmed": True,
        "timestamp": datetime.utcnow().isoformat()
    }
    return {
        "success": True,
        "message": f"{data_type} stored: {value}",
        "data_type": data_type,
        "value": value
    }

async def end_call(call_id: str) -> Dict[str, Any]:
    """
    End the call.
    
    Args:
        call_id: The call identifier
        
    Returns:
        Dictionary with success status
    """
    CALL_END_FLAGS[call_id] = True
    return {"success": True, "message": f"Call {call_id} ending"}

TOOLS = {
    "store_customer_data": store_customer_data,
    "end_call": end_call,
}

TOOLS_CONFIG = [
    {
        "type": "function",
        "name": "store_customer_data",
        "description": "Store collected customer booking information during the call",
        "parameters": {
            "type": "object",
            "properties": {
                "call_id": {"type": "string", "description": "The call identifier"},
                "data_type": {
                    "type": "string",
                    "description": "Type of data: service_type, issue_details, customer_name, preferred_date, home_address"
                },
                "value": {"type": "string", "description": "The actual value provided by the customer"}
            },
            "required": ["call_id", "data_type", "value"]
        }
    },
    {
        "type": "function",
        "name": "end_call",
        "description": "End the call after saying goodbye",
        "parameters": {
            "type": "object",
            "properties": {
                "call_id": {"type": "string", "description": "The call identifier"}
            },
            "required": ["call_id"]
        }
    }
]