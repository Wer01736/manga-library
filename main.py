if __name__ == "__main__":
    import uvicorn

    port = 8765
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=False)
