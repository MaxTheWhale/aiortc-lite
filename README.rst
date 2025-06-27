What is ``aiortc-lite``?
-------------------

The original ``aiortc`` is a library for `Web Real-Time Communication (WebRTC)`_ and
`Object Real-Time Communication (ORTC)`_ in Python. It is built on top of
``asyncio``, Python's standard asynchronous I/O framework.

``aiortc-lite`` is a lightweight version with support for only datachannels. This
allows the library to run in resource constrained embedded environments without
requiring heavy AV dependencies.

.. _Web Real-Time Communication (WebRTC): https://webrtc.org/
.. _Object Real-Time Communication (ORTC): https://ortc.org/

License
-------

``aiortc`` is released under the `BSD license`_.

.. _BSD license: https://aiortc.readthedocs.io/en/latest/license.html
