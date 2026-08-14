from typing import Annotated, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class CommentsAPI(DummyJSONBaseAPI):
    """Comment APIs

    https://dummyjson.com/docs/comments
    """

    @endpoint.get("/comments")
    def list_comments(
        self, limit: int = Unset, skip: int = Unset, select: str = Unset, **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """List comments, with optional pagination and field selection

        :param limit: Maximum number of comments to return
        :param skip: Number of comments to skip, for pagination
        :param select: Comma-separated field names to include in each comment, e.g. `body,userId`
        """
        ...

    @endpoint.get("/comments/{comment_id}")
    def get_comment(self, comment_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a comment by ID

        :param comment_id: ID of the comment to fetch
        """
        ...

    @endpoint.get("/comments/post/{post_id}")
    def list_comments_by_post(self, post_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List comments on a post

        :param post_id: ID of the post whose comments to list
        """
        ...

    @endpoint.post("/comments/add")
    def create_comment(
        self,
        body: str,
        post_id: Annotated[int, Alias("postId")],
        user_id: Annotated[int, Alias("userId")],
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Create a comment

        :param body: Comment text
        :param post_id: ID of the post to comment on
        :param user_id: ID of the user posting the comment
        """
        ...

    @endpoint.put("/comments/{comment_id}")
    def update_comment(self, comment_id: int, body: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Replace a comment

        :param comment_id: ID of the comment to update
        :param body: New comment text
        """
        ...

    @endpoint.patch("/comments/{comment_id}")
    def patch_comment(self, comment_id: int, body: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Partially update a comment

        :param comment_id: ID of the comment to update
        :param body: New comment text
        """
        ...

    @endpoint.delete("/comments/{comment_id}")
    def delete_comment(self, comment_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a comment

        :param comment_id: ID of the comment to delete
        """
        ...
