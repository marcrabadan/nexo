# The image of examples/deploy/shop.nexo: the app compiled to a native
# binary that needs only glibc. build_image("shop") in the pipeline runs
#     docker build -t shop:VERSION .
# here; run it with
#     docker run -p 8080:8080 -e API_KEY=... shop:VERSION
# Secrets come from the environment when the app runs, never from the image.

# 1. build: the Nexo compiler, then the app (its tests first)
FROM rust:1-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends clang && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/marcrabadan/nexo.git /nexo \
 && cd /nexo/compiler && cargo build --release
WORKDIR /app
COPY shop.nexo .
RUN /nexo/compiler/target/release/nexo test shop.nexo \
 && /nexo/compiler/target/release/nexo build --release shop.nexo -o shop

# 2. run: the binary on the same Debian, with the certificates for HTTPS
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/shop /usr/local/bin/shop
EXPOSE 8080
USER nobody
CMD ["shop"]
