"""LangGraph order workflow: auth, status, and gated modifications."""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional, TypedDict

from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

import orders

logger = logging.getLogger(__name__)


class OrderState(TypedDict, total=False):
    """Call-scoped business state. PCM stays outside the graph."""

    order_id: Optional[str]
    authenticated: bool
    fulfillment_status: Optional[Literal["unfulfilled", "fulfilled"]]
    shipping_status: Optional[str]
    shipping_summary: Optional[str]
    last_result: dict[str, Any]


class OrderSession:
    """
    Per-call LangGraph session.

    Gemini Live owns speech; this graph owns order facts and fulfillment policy.
    """

    def __init__(self, *, thread_id: str):
        self.thread_id = thread_id
        self._ctx: dict[str, Any] = {
            "order_id": None,
            "authenticated": False,
            "fulfillment_status": None,
            "shipping_status": None,
            "shipping_summary": None,
        }
        self._tools = self._build_tools()
        self._tools_by_name = {t.name: t for t in self._tools}
        self._graph = self._build_graph()
        self._config = {"configurable": {"thread_id": thread_id}}

    def _build_tools(self):
        ctx = self._ctx

        @tool
        def authenticate_order(order_id: str, zip_code: str) -> dict[str, Any]:
            """Verify a demo order id and zip against the local mock database."""
            result = orders.authenticate(order_id, zip_code)
            if result.get("authenticated"):
                ctx["order_id"] = result["order_id"]
                ctx["authenticated"] = True
                ctx["fulfillment_status"] = result["fulfillment_status"]
                ctx["shipping_status"] = result["shipping_status"]
                ctx["shipping_summary"] = result["shipping_summary"]
            else:
                ctx["authenticated"] = False
                ctx["order_id"] = None
            return result

        @tool
        def check_fulfillment_status() -> dict[str, Any]:
            """Return fulfillment status. Call before address changes or cancellations."""
            if not ctx.get("authenticated") or not ctx.get("order_id"):
                return {
                    "ok": False,
                    "error": "not_authenticated",
                    "agent_speak_summary": (
                        "Please share your order number and zip so I can look that up."
                    ),
                }
            order = orders.get_order(ctx["order_id"])
            if not order:
                return {
                    "ok": False,
                    "error": "order_not_found",
                    "agent_speak_summary": "I couldn't find that order anymore.",
                }
            ctx["fulfillment_status"] = order["fulfillment_status"]
            ctx["shipping_status"] = order["shipping_status"]
            allowed = order["fulfillment_status"] == "unfulfilled"
            return {
                "ok": True,
                "order_id": order["order_id"],
                "fulfillment_status": order["fulfillment_status"],
                "shipping_status": order["shipping_status"],
                "modifications_allowed": allowed,
                "agent_speak_summary": (
                    "This order is still unfulfilled, so I can change the address or cancel it."
                    if allowed
                    else (
                        "This order is already fulfilled, so I can't change the address "
                        "or cancel it — I can only help with status."
                    )
                ),
            }

        @tool
        def change_shipping_address(
            line1: str,
            city: str,
            state: str,
            zip_code: str,
            line2: str = "",
        ) -> dict[str, Any]:
            """Update shipping address when fulfillment_status is unfulfilled."""
            if not ctx.get("authenticated") or not ctx.get("order_id"):
                return {
                    "ok": False,
                    "error": "not_authenticated",
                    "agent_speak_summary": (
                        "Please verify your order with the order number and zip first."
                    ),
                }
            result = orders.update_address(
                ctx["order_id"],
                {
                    "line1": line1,
                    "line2": line2,
                    "city": city,
                    "state": state,
                    "zip": zip_code,
                },
            )
            if result.get("ok"):
                ctx["fulfillment_status"] = result.get("fulfillment_status")
            return result

        @tool
        def cancel_order() -> dict[str, Any]:
            """Cancel the authenticated order when fulfillment_status is unfulfilled."""
            if not ctx.get("authenticated") or not ctx.get("order_id"):
                return {
                    "ok": False,
                    "error": "not_authenticated",
                    "agent_speak_summary": (
                        "Please verify your order with the order number and zip first."
                    ),
                }
            result = orders.cancel_order(ctx["order_id"])
            if result.get("ok") and result.get("cancelled"):
                ctx["shipping_status"] = "cancelled"
            return result

        @tool
        def hang_up() -> dict[str, Any]:
            """End the phone call after the caller says goodbye, bye, or asks to hang up."""
            return {
                "ok": True,
                "hang_up": True,
                "agent_speak_summary": "Goodbye.",
            }

        return [
            authenticate_order,
            check_fulfillment_status,
            change_shipping_address,
            cancel_order,
            hang_up,
        ]

    def _build_graph(self):
        def persist(state: OrderState) -> OrderState:
            return {
                "order_id": self._ctx.get("order_id"),
                "authenticated": bool(self._ctx.get("authenticated")),
                "fulfillment_status": self._ctx.get("fulfillment_status"),
                "shipping_status": self._ctx.get("shipping_status"),
                "shipping_summary": self._ctx.get("shipping_summary"),
                "last_result": state.get("last_result") or {},
            }

        builder = StateGraph(OrderState)
        builder.add_node("persist", persist)
        builder.add_edge(START, "persist")
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=MemorySaver())

    def gemini_declarations(self):
        """Build google-genai FunctionDeclarations from LangChain tools."""
        from google.genai import types

        decls = []
        for t in self._tools:
            schema = (
                t.args_schema.model_json_schema()
                if t.args_schema is not None
                else {"type": "object", "properties": {}}
            )
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            clean_props: dict[str, Any] = {}
            for key, val in props.items():
                if not isinstance(val, dict):
                    continue
                entry: dict[str, Any] = {"type": val.get("type", "string")}
                if val.get("description"):
                    entry["description"] = val["description"]
                if "anyOf" in val and not val.get("type"):
                    for option in val["anyOf"]:
                        if isinstance(option, dict) and option.get("type") not in (
                            None,
                            "null",
                        ):
                            entry["type"] = option["type"]
                            break
                clean_props[key] = entry
            decls.append(
                types.FunctionDeclaration(
                    name=t.name,
                    description=t.description or t.name,
                    parameters={
                        "type": "object",
                        "properties": clean_props,
                        "required": [r for r in required if r in clean_props],
                    },
                )
            )
        return decls

    async def ainvoke_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool_fn = self._tools_by_name.get(name)
        if tool_fn is None:
            return {
                "ok": False,
                "error": f"unknown tool {name}",
                "agent_speak_summary": "Something went wrong. Please say that again.",
            }

        logger.info("LangGraph tool %s args=%s thread=%s", name, args, self.thread_id)
        try:
            raw = await tool_fn.ainvoke(args or {})
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return {
                "ok": False,
                "error": str(exc),
                "agent_speak_summary": (
                    "I hit a technical issue. Please repeat your order details."
                ),
            }

        if not isinstance(raw, dict):
            raw = {"ok": True, "result": raw}

        await self._graph.ainvoke(
            {
                "order_id": self._ctx.get("order_id"),
                "authenticated": bool(self._ctx.get("authenticated")),
                "fulfillment_status": self._ctx.get("fulfillment_status"),
                "shipping_status": self._ctx.get("shipping_status"),
                "shipping_summary": self._ctx.get("shipping_summary"),
                "last_result": raw,
            },
            self._config,
        )
        return raw

    def snapshot(self) -> dict[str, Any]:
        return dict(self._ctx)
