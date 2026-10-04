**Envelope** is an object that implements the same interface, wraps a ready-made composition of other objects, and delegates all work to them.

The main goal is to **give a name to a frequently used object composition**.

For example, suppose we have several decorators:

```python
class Response:
    def render(self):
        raise NotImplementedError


class Text(Response):
    def __init__(self, text):
        self.text = text

    def render(self):
        return {"body": self.text}


class WithStatus(Response):
    def __init__(self, origin, status):
        self.origin = origin
        self.status = status

    def render(self):
        result = self.origin.render()
        return {**result, "status": self.status}


class WithContentType(Response):
    def __init__(self, origin, content_type):
        self.origin = origin
        self.content_type = content_type

    def render(self):
        result = self.origin.render()
        return {**result, "content_type": self.content_type}
```

Without an Envelope, the composition looks like this:

```python
response = WithContentType(
    WithStatus(
        Text("<h1>Hello</h1>"),
        200,
    ),
    "text/html",
)
```

We can introduce a base Envelope:

```python
class ResponseEnvelope(Response):
    def __init__(self, origin):
        self._origin = origin

    def render(self):
        return self._origin.render()
```

And give the composition a name:

```python
class HtmlResponse(ResponseEnvelope):
    def __init__(self, html):
        super().__init__(
            WithContentType(
                WithStatus(
                    Text(html),
                    200,
                ),
                "text/html",
            )
        )
```

Now the client code becomes simpler:

```python
response = HtmlResponse("<h1>Hello</h1>")

print(response.render())
```

Result:

```python
{
    "body": "<h1>Hello</h1>",
    "status": 200,
    "content_type": "text/html",
}
```

A simple way to remember the idea:

```text
Decorator → adds behavior to an object

Composition → combines several objects

Envelope → gives that composition a name
```

For example:

```python
request = Retrying(
    Authenticated(
        JsonRequest(
            HttpRequest(url)
        ),
        token,
    )
)
```

can be hidden behind:

```python
request = GithubRequest(url, token)
```

where `GithubRequest` does almost nothing by itself — it only builds the required composition and delegates calls to it.

Unlike a **Factory**, an Envelope is itself an object of the required interface:

```python
# Factory
request = RequestFactory.github(url, token)

# Envelope
request = GithubRequest(url, token)
```

The main idea of the pattern is to **express architecture through small objects and their composition, while giving recurring compositions meaningful domain names**.