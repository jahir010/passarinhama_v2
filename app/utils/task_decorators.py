def every(**kwargs):
    def decorator(func):
        func._schedule = kwargs
        return func
    return decorator
