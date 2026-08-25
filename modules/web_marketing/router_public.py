"""مسارات SEO العامة — robots.txt و sitemaps."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from app.deps import DBSession
from modules.web_marketing.seo_public import (
    build_robots_txt,
    build_sitemap_images_xml,
    build_sitemap_products_xml,
    build_sitemap_rooms_xml,
    build_sitemap_xml,
)

router = APIRouter(tags=["seo-public"])


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(db: DBSession):
    return PlainTextResponse(
        build_robots_txt(db),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/sitemap.xml")
def sitemap_xml(db: DBSession):
    return Response(
        content=build_sitemap_xml(db),
        media_type="application/xml; charset=utf-8",
    )


@router.get("/sitemap-products.xml")
def sitemap_products_xml(db: DBSession):
    return Response(
        content=build_sitemap_products_xml(db),
        media_type="application/xml; charset=utf-8",
    )


@router.get("/sitemap-rooms.xml")
def sitemap_rooms_xml(db: DBSession):
    return Response(
        content=build_sitemap_rooms_xml(db),
        media_type="application/xml; charset=utf-8",
    )


@router.get("/sitemap-images.xml")
def sitemap_images_xml(db: DBSession):
    return Response(
        content=build_sitemap_images_xml(db),
        media_type="application/xml; charset=utf-8",
    )
