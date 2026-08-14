from typing import Annotated, Literal, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class PostsAPI(DummyJSONBaseAPI):
    """Post APIs

    https://dummyjson.com/docs/posts
    """

    @endpoint.get("/posts")
    def list_posts(
        self,
        limit: int = Unset,
        skip: int = Unset,
        select: str = Unset,
        sort_by: Annotated[str, Alias("sortBy")] = Unset,
        order: Literal["asc", "desc"] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """List posts, with optional pagination, field selection, and sorting

        :param limit: Maximum number of posts to return
        :param skip: Number of posts to skip, for pagination
        :param select: Comma-separated field names to include in each post, e.g. `title,body`
        :param sort_by: Field name to sort by, e.g. `title`
        :param order: Sort direction to apply when `sort_by` is given
        """
        ...

    @endpoint.get("/posts/{post_id}")
    def get_post(self, post_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a post by ID

        :param post_id: ID of the post to fetch
        """
        ...

    @endpoint.get("/posts/search")
    def search_posts(self, q: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Search posts by query term

        :param q: Search query term
        """
        ...

    @endpoint.get("/posts/tags")
    def list_post_tags(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List all post tags with their slugs and post counts"""
        ...

    @endpoint.get("/posts/tag-list")
    def list_post_tag_names(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List all post tag names"""
        ...

    @endpoint.get("/posts/tag/{tag}")
    def list_posts_by_tag(self, tag: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List posts tagged with a given tag

        :param tag: Tag to filter posts by
        """
        ...

    @endpoint.get("/posts/user/{user_id}")
    def list_posts_by_user(self, user_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List posts authored by a user

        :param user_id: ID of the user whose posts to list
        """
        ...

    @endpoint.get("/posts/{post_id}/comments")
    def list_post_comments(self, post_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List comments on a post

        :param post_id: ID of the post whose comments to list
        """
        ...

    @endpoint.post("/posts/add")
    def create_post(
        self, title: str, user_id: Annotated[int, Alias("userId")], **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """Create a post

        :param title: Post title
        :param user_id: ID of the user authoring the post
        """
        ...

    @endpoint.put("/posts/{post_id}")
    def update_post(self, post_id: int, title: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Replace a post

        :param post_id: ID of the post to update
        :param title: New post title
        """
        ...

    @endpoint.patch("/posts/{post_id}")
    def patch_post(self, post_id: int, title: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Partially update a post

        :param post_id: ID of the post to update
        :param title: New post title
        """
        ...

    @endpoint.delete("/posts/{post_id}")
    def delete_post(self, post_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a post

        :param post_id: ID of the post to delete
        """
        ...
